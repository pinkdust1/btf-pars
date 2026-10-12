import asyncio
import os
import re
from pathlib import Path
from urllib.parse import urlparse, unquote

# На Render браузеры, скачанные на этапе сборки в ~/.cache, до запуска не доживают.
# PLAYWRIGHT_BROWSERS_PATH=0 кладёт их внутрь пакета (в .venv), и они сохраняются.
# ВАЖНО: тот же флаг должен стоять и в build command (см. инструкцию).
if os.getenv("RENDER") and not os.getenv("PLAYWRIGHT_BROWSERS_PATH"):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"

# patchright - форк Playwright с патчами против детекта автоматизации.
# Если он не установлен, используем обычный playwright (+ playwright-stealth, если есть).
#   pip install patchright playwright-stealth
#   patchright install chrome
try:
    from patchright.async_api import (
        async_playwright,
        Page,
        Frame,
        Locator,
        BrowserContext,
        TimeoutError as PlaywrightTimeout,
    )
    ENGINE = "patchright"
except ImportError:
    from playwright.async_api import (
        async_playwright,
        Page,
        Frame,
        Locator,
        BrowserContext,
        TimeoutError as PlaywrightTimeout,
    )
    ENGINE = "playwright"

URL = "https://perchance.org/beautiful-people"

# Если кнопка запуска не находится автоматически, впиши сюда её текст из списка кнопок,
# который скрипт печатает в консоль (например "generate" или "Create").
GENERATE_BUTTON_TEXT: str | None = None

def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() not in ("0", "false", "no", "off", "")


# Настройки берутся из переменных окружения (удобно для хостинга), иначе - значения по умолчанию.
#   HEADLESS=1        браузер без окна (по умолчанию). Для ручного прохождения капчи локально: HEADLESS=0
#   PROFILE_DIR=...   папка постоянного профиля (cookies, cf_clearance)
#   USE_REAL_CHROME=1 пробовать установленный Google Chrome, если он есть
#   PROXY_URL=...     прокси, например http://user:pass@host:port (на хостингах с IP дата-центра
#                     Cloudflare часто отдаёт 403, помогает резидентный прокси)
HEADLESS = _env_bool("HEADLESS", True)
USER_DATA_DIR = os.getenv("PROFILE_DIR") or str(Path(__file__).resolve().parent / "profile")
USE_REAL_CHROME = _env_bool("USE_REAL_CHROME", True)
PROXY_URL = os.getenv("PROXY_URL") or None


# --------------------------------------------------------------------------- #
#  Анти-детект / запуск браузера
# --------------------------------------------------------------------------- #

async def apply_stealth(context: BrowserContext, page: Page) -> None:
    """Скрываем признаки автоматизации."""
    # navigator.webdriver -> undefined
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    if ENGINE == "patchright":
        return  # patchright уже содержит свои патчи

    # Для обычного playwright пробуем подключить playwright-stealth (поддерживаем v1 и v2)
    try:
        from playwright_stealth import Stealth  # v2.x

        await Stealth().apply_stealth_async(context)
        print("   ✓ playwright-stealth (v2) применён")
        return
    except ImportError:
        pass
    except Exception as e:
        print(f"   ⚠️ playwright-stealth v2 не применился: {e}")

    try:
        from playwright_stealth import stealth_async  # v1.x

        await stealth_async(page)
        print("   ✓ playwright-stealth (v1) применён")
    except ImportError:
        print("   (playwright-stealth не установлен, пропускаем)")
    except Exception as e:
        print(f"   ⚠️ playwright-stealth v1 не применился: {e}")


def _proxy_settings() -> dict | None:
    if not PROXY_URL:
        return None
    u = urlparse(PROXY_URL)
    cfg = {"server": f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")}
    if u.username:
        cfg["username"] = unquote(u.username)
    if u.password:
        cfg["password"] = unquote(u.password)
    return cfg


async def launch_context(p) -> BrowserContext:
    """Постоянный профиль + (по возможности) реальный Chrome + флаги против детекта."""
    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)

    common = dict(
        user_data_dir=USER_DATA_DIR,
        headless=HEADLESS,
        viewport={"width": 1280, "height": 900},
        # Свой user_agent НЕ задаём: родной UA браузера совпадает с Client Hints.
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
        ],
        ignore_default_args=["--enable-automation"],
    )
    proxy = _proxy_settings()
    if proxy:
        common["proxy"] = proxy
        print(f"   Прокси: {proxy['server']}")

    # Порядок: установленный Chrome -> полный Chromium (новый headless, меньше палится,
    # чем headless-shell) -> то, что выберет сам движок.
    channels: list[str | None] = (["chrome"] if USE_REAL_CHROME else []) + ["chromium", None]
    last_err = None
    for ch in channels:
        try:
            kwargs = dict(common)
            if ch:
                kwargs["channel"] = ch
            ctx = await p.chromium.launch_persistent_context(**kwargs)
            mode = "headless" if HEADLESS else "с окном"
            print(f"   Движок: {ENGINE}, браузер: {ch or 'по умолчанию'}, режим: {mode}")
            return ctx
        except Exception as e:
            last_err = e
            print(f"   ⚠️ {ch or 'по умолчанию'} не запустился: {str(e).splitlines()[0]}")
    if "Executable doesn't exist" in str(last_err):
        print("   ❌ Браузер не установлен там, где его ищет скрипт.")
        print("      Build command: PLAYWRIGHT_BROWSERS_PATH=0 patchright install chromium")
    raise RuntimeError(f"Не удалось запустить ни один браузер: {last_err}")


# --------------------------------------------------------------------------- #
#  Cloudflare / блокировки
# --------------------------------------------------------------------------- #

def is_blocked(page: Page) -> bool:
    """Есть ли фрейм, который не загрузился (403 / ERR_BLOCKED_BY_RESPONSE -> chrome-error)."""
    return any(fr.url.startswith("chrome-error") for fr in page.frames)


async def has_cloudflare_challenge(page: Page) -> bool:
    """Видна ли на основной странице проверка Cloudflare ('Just a moment...', Turnstile)."""
    try:
        title = (await page.title()).lower()
        if "just a moment" in title or "attention required" in title:
            return True
        body = await page.evaluate(
            "() => (document.body ? document.body.innerText : '').slice(0, 400).toLowerCase()"
        )
        return any(
            s in body
            for s in ("verify you are human", "checking your browser", "just a moment")
        )
    except Exception:
        return False


async def wait_for_manual_challenge(page: Page, reason: str) -> bool:
    """
    Если окно браузера видно - просим пройти проверку руками и ждём Enter.
    В headless-режиме это невозможно, поэтому возвращаем False.
    """
    if HEADLESS:
        print(f"   ⚠️ {reason}")
        print("      Headless-режим: ручная проверка невозможна. Попробуй другой IP/прокси (PROXY_URL),")
        print("      либо один раз пройди проверку локально (HEADLESS=0) и перенеси папку профиля.")
        return False

    print(f"   ⚠️ {reason}")
    print("      >>> Пройди проверку в окне браузера (капча / галочка), дождись загрузки")
    print("      >>> страницы и нажми Enter здесь, в консоли.")
    await asyncio.to_thread(input, "      Enter, когда готово... ")
    return True


# --------------------------------------------------------------------------- #
#  Хелперы поиска / отладки
# --------------------------------------------------------------------------- #

async def find_frame(
    page: Page, selector: str, timeout: float = 10.0, state: str = "visible"
) -> Frame | None:
    """
    Ищет элемент по селектору в основной странице И во всех iframe.
    state="visible"  - элемент должен быть виден,
    state="attached" - достаточно, чтобы он просто был в DOM (может быть скрыт).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        for frame in page.frames:
            try:
                loc = frame.locator(selector).first
                if state == "attached":
                    if await loc.count() > 0:
                        return frame
                elif await loc.is_visible():
                    return frame
            except Exception:
                continue  # фрейм мог отсоединиться во время обхода
        await asyncio.sleep(0.25)
    return None


async def dump_frames(page: Page, selector: str, tag: str = "debug") -> None:
    """Отладка: что лежит в каждом фрейме + сохраняет HTML фреймов в файлы."""
    await page.screenshot(path=f"perchance_{tag}.png")
    print(f"   Скриншот: perchance_{tag}.png")
    for i, fr in enumerate(page.frames):
        try:
            info = await fr.evaluate(
                """sel => ({
                    url: location.href.slice(0, 80),
                    found: document.querySelectorAll(sel).length,
                    textareas: [...document.querySelectorAll('textarea')].map(t => t.id || '(без id)'),
                    hasPromptDiv: !!document.getElementById('promptControllerDiv'),
                    hasButtonsDiv: !!document.getElementById('buttons'),
                    body: (document.body ? document.body.innerText : '').trim().slice(0, 120)
                })""",
                selector,
            )
            print(f"   frame[{i}]: {info}")
            html = await fr.content()
            with open(f"debug_frame_{i}.html", "w", encoding="utf-8") as f:
                f.write(html)
        except Exception as e:
            print(f"   frame[{i}] недоступен: {e}")
    print("   HTML фреймов сохранён в debug_frame_N.html")


def attach_network_logging(page: Page) -> None:
    """Печатает упавшие запросы и навигации во фреймах, чтобы понять, почему iframe не грузится."""

    def on_failed(req):
        failure = req.failure or ""
        # ERR_ABORTED у картинок/трекеров - обычный шум, его пропускаем
        if req.resource_type == "document" or "ERR_ABORTED" not in str(failure):
            print(f"   [net-fail] {req.resource_type} {req.url[:130]} -> {failure}")

    def on_response(resp):
        if resp.status >= 400:
            print(f"   [http {resp.status}] {resp.request.resource_type} {resp.url[:130]}")

    def on_nav(frame):
        if frame != page.main_frame:
            print(f"   [iframe] {frame.url[:130]}")

    page.on("requestfailed", on_failed)
    page.on("response", on_response)
    page.on("framenavigated", on_nav)


async def click_text(scope: Frame | Locator, text: str, timeout: int = 8000) -> None:
    """Кликает по <button> с текстом, а если такой кнопки нет — по любому элементу с этим текстом."""
    btn = scope.locator(f'button:has-text("{text}")').first
    if await btn.count() == 0:
        btn = scope.get_by_text(text).first
    await btn.click(timeout=timeout)


async def dump_selects(page: Page) -> None:
    """Отладка: печатает все <select> во всех фреймах (id, видимость, варианты)."""
    for i, fr in enumerate(page.frames):
        try:
            info = await fr.evaluate(
                """() => [...document.querySelectorAll('select')].map(s => ({
                    id: s.id,
                    visible: !!(s.offsetWidth || s.offsetHeight),
                    options: [...s.options].map(o => o.text.trim())
                }))"""
            )
            print(f"   frame[{i}] {fr.url[:70]}: {info}")
        except Exception as e:
            print(f"   frame[{i}] недоступен: {e}")


# --------------------------------------------------------------------------- #
#  Content Warning
# --------------------------------------------------------------------------- #

async def pass_content_gate(page: Page) -> bool:
    """
    Content Warning -> change preferences -> выпадашка Warn -> чекбокс 18+
    -> Save -> снова Content Warning -> "I am over 18 - Show Content".
    """
    print("2. Ищем Content Warning...")
    frame = await find_frame(page, "text=Content Warning", timeout=10)
    if frame is None:
        print("   Предупреждения нет, идём дальше")
        return True

    # --- change preferences ---
    print("3. Открываем настройки...")
    await frame.locator("text=change preferences").first.click()

    frame = await find_frame(page, "text=User Content Preferences", timeout=10)
    if frame is None:
        print("   ❌ Окно настроек не открылось")
        await page.screenshot(path="perchance_debug_prefs.png")
        return False

    # --- выпадашка: выбираем Warn ---
    print("   - Ищем выпадашку с Warn...")
    # На странице есть и другие <select> (например скрытый consoleInterpreterOption),
    # поэтому берём именно тот, в котором есть option "Warn".
    frame = await find_frame(page, 'select:has-text("Warn")', timeout=10)
    if frame is None:
        print("   ❌ Выпадашка с Warn не найдена. Все select на странице:")
        await dump_selects(page)
        await page.screenshot(path="perchance_debug_select.png")
        return False

    select = frame.locator('select:has-text("Warn")').first

    # Окно настроек = ближайший родитель этого select, внутри которого есть заголовок.
    # Дальше ищем чекбокс и Save только внутри него, чтобы не попасть в чужие элементы страницы.
    dialog = select.locator("xpath=ancestor::*[contains(., 'User Content Preferences')][1]")
    if await dialog.count() == 0:
        dialog = frame.locator("body")

    warn_value = await select.evaluate(
        """s => {
            const o = [...s.options].find(o => /warn/i.test(o.text) || /warn/i.test(o.value));
            return o ? o.value : null;
        }"""
    )
    if warn_value is None:
        print("   ❌ Option 'Warn' не найден")
        await dump_selects(page)
        return False

    # select_option сам генерирует настоящие input + change события
    await select.select_option(value=warn_value)
    print(f"   ✓ Выбрано Warn (value={warn_value!r})")

    # --- ждём чекбокс 18+ (он появляется только после выбора Warn) ---
    print("   - Ждём чекбокс 18+...")
    label = dialog.locator("text=I am over 18 years old").first
    try:
        await label.wait_for(state="visible", timeout=5000)
    except PlaywrightTimeout:
        # запасной вариант: вручную отправляем события, если страница слушает их иначе
        await select.evaluate(
            """s => {
                s.dispatchEvent(new Event('input',  {bubbles: true}));
                s.dispatchEvent(new Event('change', {bubbles: true}));
            }"""
        )
        try:
            await label.wait_for(state="visible", timeout=5000)
        except PlaywrightTimeout:
            print("   ❌ Чекбокс не появился")
            await page.screenshot(path="perchance_debug_checkbox.png")
            return False

    checkbox = dialog.locator('input[type="checkbox"]').first
    try:
        await checkbox.check(timeout=3000)  # сам проверит, что галочка реально стоит
    except Exception:
        await label.click()  # если input скрыт стилями — кликаем по подписи
    print("   ✓ Галочка 18+ поставлена")

    # --- Save ---
    print("   - Жмём Save...")
    await click_text(dialog, "Save")
    print("   ✓ Save нажат")

    # --- снова Content Warning: "I am over 18 - Show Content" ---
    print("4. Подтверждаем возраст...")
    frame = await find_frame(page, "text=Show Content", timeout=10)
    if frame is None:
        print("   ❌ Кнопка 'Show Content' не появилась")
        await page.screenshot(path="perchance_debug_show_content.png")
        return False

    await click_text(frame, "Show Content")
    print("   ✓ Подтверждено: I am over 18 - Show Content")

    # ждём, пока экран предупреждения исчезнет
    try:
        await frame.get_by_text("Content Warning").first.wait_for(state="hidden", timeout=10000)
    except Exception:
        pass
    return True


# --------------------------------------------------------------------------- #
#  Ожидание результата
# --------------------------------------------------------------------------- #

_FIND_BIG_IMAGE_JS = """() => {
    let best = null;
    for (const im of document.images) {
        if (!im.complete) continue;
        const w = im.naturalWidth, h = im.naturalHeight;
        if (w < 256 || h < 256) continue;
        const area = w * h;
        if (!best || area > best.area) best = {src: im.currentSrc || im.src, w, h, area};
    }
    return best;
}"""

_TO_DATA_URL_JS = """async src => {
    if (src.startsWith('data:')) return src;
    const r = await fetch(src);
    const b = await r.blob();
    return await new Promise((res, rej) => {
        const f = new FileReader();
        f.onload = () => res(f.result);
        f.onerror = rej;
        f.readAsDataURL(b);
    });
}"""


def _is_result_frame(fr: Frame) -> bool:
    """Результат рисуется во фрейме image-generation.perchance.org/embed (не в /gallery)."""
    u = fr.url
    return "image-generation.perchance.org" in u and "/gallery" not in u


async def wait_for_result_image(page: Page, timeout: float = 120.0):
    """Опрашивает все фреймы генератора, пока не появится большая картинка. -> (frame, info) | None"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        best = None
        for fr in page.frames:
            if not _is_result_frame(fr):
                continue
            try:
                info = await fr.evaluate(_FIND_BIG_IMAGE_JS)
            except Exception:
                continue  # фрейм навигируется / отсоединился
            if info and (best is None or info["area"] > best[1]["area"]):
                best = (fr, info)
        if best:
            return best
        await asyncio.sleep(1)
    return None


async def dump_images(page: Page) -> None:
    """Отладка: все <img> и <canvas> во всех фреймах."""
    for i, fr in enumerate(page.frames):
        try:
            info = await fr.evaluate(
                """() => ({
                    imgs: [...document.images].map(im => ({
                        src: (im.currentSrc || im.src).slice(0, 80),
                        w: im.naturalWidth, h: im.naturalHeight
                    })),
                    canvases: document.querySelectorAll('canvas').length
                })"""
            )
            print(f"   frame[{i}] {fr.url[:70]}: {info}")
        except Exception as e:
            print(f"   frame[{i}] недоступен: {e}")


async def save_image(fr: Frame, src: str, path_base: str = "perchance_result") -> tuple[str, str | None]:
    """Превращает src (data:/blob:/https:) в data URL, сохраняет файл. -> (data_url_or_src, path|None)"""
    import base64

    try:
        data_url = await fr.evaluate(_TO_DATA_URL_JS, src)
    except Exception as e:
        print(f"   ⚠️ Не удалось скачать картинку внутри фрейма ({e}), возвращаем src как есть")
        return src, None

    m = re.match(r"data:image/(\w+);base64,(.*)", data_url, re.S)
    if not m:
        return data_url, None
    ext = "jpg" if m.group(1) == "jpeg" else m.group(1)
    path = f"{path_base}.{ext}"
    with open(path, "wb") as f:
        f.write(base64.b64decode(m.group(2)))
    return data_url, path


# --------------------------------------------------------------------------- #
#  Основная логика
# --------------------------------------------------------------------------- #

async def generate_perchance_image(prompt: str, style: str = "NSFW") -> str | None:
    async with async_playwright() as p:
        print("0. Запускаем браузер...")
        context = await launch_context(p)
        page = context.pages[0] if context.pages else await context.new_page()
        await apply_stealth(context, page)
        attach_network_logging(page)

        try:
            print("1. Открываем страницу...")
            await page.goto(URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

            # Cloudflare-проверка на основной странице
            if await has_cloudflare_challenge(page):
                if not await wait_for_manual_challenge(
                    page, "Cloudflare показал проверку на основной странице."
                ):
                    await page.screenshot(path="perchance_cf_challenge.png")
                    return None
                await page.reload(wait_until="domcontentloaded")
                await asyncio.sleep(2)

            # 2-4. Проходим Content Warning
            if not await pass_content_gate(page):
                return None

            # 5. Ищем поле ввода: #promptControllerDiv > textarea#promptControllerTextarea
            print("5. Ищем поле ввода...")
            sel_input = "#promptControllerTextarea"
            gen = await find_frame(page, sel_input, timeout=30, state="attached")

            if gen is None and is_blocked(page):
                print("   ⚠️ iframe генератора заблокирован (403 / Cloudflare, chrome-error).")
                # Даём шанс пройти проверку руками (только если окно видно),
                # затем перезагружаем страницу.
                if await wait_for_manual_challenge(
                    page,
                    "Нужно пройти бот-проверку Cloudflare (или сменить IP / отключить VPN).",
                ):
                    pass
                print("   Перезагружаем страницу...")
                await page.reload(wait_until="domcontentloaded")
                await asyncio.sleep(2)
                if not await pass_content_gate(page):
                    return None
                print("5. Повторно ищем поле ввода...")
                gen = await find_frame(page, sel_input, timeout=30, state="attached")

            if gen is None:
                if is_blocked(page):
                    print("   ❌ iframe генератора всё ещё заблокирован (403).")
                    print("      Что ещё можно сделать: отключить VPN/прокси, сменить сеть,")
                    print("      удалить папку profile и пройти проверку заново вручную.")
                else:
                    print("   ❌ Элемента нет ни в одном фрейме за 30 сек")
                await dump_frames(page, sel_input, tag="no_input")
                return None

            print("   ✓ Элемент есть в DOM, ждём пока станет видимым...")
            try:
                await gen.locator(sel_input).first.wait_for(state="visible", timeout=15000)
            except PlaywrightTimeout:
                hidden = await gen.locator(sel_input).first.evaluate(
                    """el => {
                        const out = [];
                        for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
                            const cs = getComputedStyle(n);
                            if (cs.display === 'none' || cs.visibility === 'hidden' || n.hidden
                                || (n.offsetWidth === 0 && n.offsetHeight === 0))
                                out.push(`<${n.tagName.toLowerCase()} id="${n.id}" class="${n.className}"> display=${cs.display} visibility=${cs.visibility}`);
                        }
                        return out;
                    }"""
                )
                print("   ❌ Элемент в DOM, но скрыт. Скрывающие родители:")
                for h in hidden:
                    print(f"      {h}")
                await dump_frames(page, sel_input, tag="input_hidden")
                return None
            print("   ✓ Поле найдено и видно")

            # 6. Вводим промпт
            print(f"6. Вводим промпт: {prompt}")
            input_field = gen.locator("#promptControllerTextarea").first
            await input_field.fill(prompt)  # fill сам кликает и генерит input-события
            print("   ✓ Промпт введён")

            # 7. Выводим все кнопки из div#buttons
            print("7. Кнопки в #buttons:")
            buttons = await gen.evaluate(
                """() => [...document.querySelectorAll('#buttons button')].map(b => ({
                    id: b.id,
                    text: (b.innerText || b.textContent || '').trim(),
                    title: b.title || '',
                    disabled: b.disabled
                }))"""
            )
            if not buttons:
                print("   ❌ В #buttons нет ни одной кнопки")
                await page.screenshot(path="perchance_debug_no_buttons.png")
                return None
            for i, b in enumerate(buttons):
                print(f"   [{i}] text={b['text']!r} id={b['id']!r} title={b['title']!r} disabled={b['disabled']}")

            btns = gen.locator("#buttons button")

            # 8. Кнопки стилей И ЕСТЬ кнопки запуска: клик по стилю сразу стартует генерацию.
            if style:
                print(f"8. Кликаем кнопку стиля (запускает генерацию): {style}")
                # Точное совпадение текста, чтобы "NSFW" не совпал с "NSFWAnime"/"NSFWAnthro"
                style_btn = btns.filter(
                    has_text=re.compile(rf"^\s*{re.escape(style)}\s*$")
                ).first
                if await style_btn.count() == 0:
                    print(f"   ❌ Кнопка стиля {style!r} не найдена. Доступные - в списке выше.")
                    await page.screenshot(path="perchance_debug_no_style.png")
                    return None
                await style_btn.click()
                print("   ✓ Генерация запущена")
            else:
                print("8. Стиль не задан - ищем обычную кнопку запуска...")
                if GENERATE_BUTTON_TEXT:
                    gen_btn = btns.filter(has_text=GENERATE_BUTTON_TEXT).first
                else:
                    gen_btn = btns.filter(
                        has_text=re.compile(r"generate|create|go|submit|send|run", re.I)
                    ).first
                if await gen_btn.count() == 0:
                    print("   ❌ Кнопка запуска не найдена. Задай style (например 'NSFW').")
                    await page.screenshot(path="perchance_debug_no_generate.png")
                    return None
                await gen_btn.click()
                print("   ✓ Генерация запущена")

            # 9. Ждём картинку во фрейме image-generation.perchance.org/embed
            print("9. Ожидаем результат (до 120 сек)...")
            found = await wait_for_result_image(page, timeout=120)
            if found is None:
                print("   ❌ Таймаут ожидания изображения. Что есть на странице:")
                await dump_images(page)
                await dump_frames(page, "img", tag="timeout")
                await page.screenshot(path="perchance_timeout.png")
                return None

            fr, info = found
            print(f"   ✓ Картинка найдена: {info['w']}x{info['h']}, src={info['src'][:60]}...")
            await asyncio.sleep(3)  # даём дорисоваться/дозагрузиться финальной версии

            # берём картинку ещё раз после паузы (могла смениться превью -> финал)
            try:
                info = await fr.evaluate(_FIND_BIG_IMAGE_JS) or info
            except Exception:
                pass

            image_url, saved_path = await save_image(fr, info["src"])
            await page.screenshot(path="perchance_success.png")
            if saved_path:
                print(f"   ✓ Изображение сохранено: {saved_path}")
            print("   ✓ Скриншот страницы: perchance_success.png")
            return image_url

        except Exception as e:
            print(f"❌ Ошибка: {e}")
            try:
                await page.screenshot(path="perchance_error.png")
            except Exception:
                pass
            return None
        finally:
            await context.close()


if __name__ == "__main__":
    result = asyncio.run(
        generate_perchance_image(
            prompt="beautiful woman, portrait, detailed",
            style="NSFW",
        )
    )

    if result:
        print(f"\n✅ Результат: {result[:100]}...")
    else:
        print("\n❌ Не удалось сгенерировать")