#!/usr/bin/env python3
"""Detecta y descarga vídeos públicos reproducibles de una serie de AskiDrama.

Uso:
    python descargar_askidrama.py --inicio 1 --fin 45
    python descargar_askidrama.py --inicio 1 --fin 1 --diagnostico

No abre anuncios, no acepta descargas del navegador ni instala software
ofrecido por el sitio. Requiere: yt-dlp, playwright, Chromium para Playwright.
"""

import argparse
import asyncio
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

try:
    import yt_dlp
    from playwright.async_api import async_playwright
except ImportError as exc:
    print(f"Falta una dependencia: {exc}\nInstala: pip install -U yt-dlp playwright")
    print("Después: python -m playwright install chromium")
    sys.exit(1)

BASE = "https://askidrama.com/es/drama/cuando-deje-de-amarte-doblado"
SALIDA = Path("Cuando_deje_de_amarte")
EXT_VIDEO = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
AD_HOSTS = (
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "adsterra.com", "propellerads.com", "exoclick.com", "popads.net",
    "adnxs.com", "trafficjunky.net", "onclicka.com", "juicyads.com",
)
STREAM_EXTS = (".m3u8", ".mp4", ".mpd", ".webm", ".m4v")
VIDEO_TYPES = ("video/", "application/vnd.apple.mpegurl",
               "application/x-mpegurl", "application/dash+xml")
URL_PATTERN = re.compile(
    r"(?:https?:)?//[^\s\"'<>\\]+?\.(?:m3u8|mp4|mpd|webm|m4v)(?:\?[^\s\"'<>\\]*)?",
    re.IGNORECASE,
)


def permitido(url: str) -> bool:
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        return False
    host = (urlparse(url).hostname or "").lower()
    return not any(host == domain or host.endswith("." + domain) for domain in AD_HOSTS)


def es_stream(url: str) -> bool:
    return urlparse(url).path.lower().endswith(STREAM_EXTS)


def buscar_en_texto(texto: str) -> list[str]:
    decoded = html.unescape(texto).replace(r"\/", "/")
    decoded = decoded.replace(r"\u0026", "&").replace(r"\u003d", "=")
    urls = []
    for m in URL_PATTERN.finditer(decoded):
        url = m.group(0)
        if url.startswith("//"):
            url = "https:" + url
        if permitido(url):
            urls.append(url)
    return urls


def existe_video(n: int) -> bool:
    return any(p.suffix.lower() in EXT_VIDEO and p.stat().st_size > 2048
               for p in SALIDA.glob(f"Capitulo_{n:02d}.*"))


def descargar_con_ytdlp(url: str, episodio: int, referer: str) -> tuple[bool, str]:
    opciones = {
        "outtmpl": str(SALIDA / f"Capitulo_{episodio:02d}.%(ext)s"),
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 18,
        "http_headers": {
            "Referer": referer,
            "Origin": "https://askidrama.com",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/131.0.0.0 Safari/537.36",
        },
    }
    try:
        with yt_dlp.YoutubeDL(opciones) as ydl:
            ydl.download([url])
        return existe_video(episodio), "No se creó un archivo de vídeo válido"
    except Exception as e:
        return False, str(e)[:250]


async def inspeccionar(browser, episodio: int, diagnostico: bool):
    pagina_url = f"{BASE}/{episodio}"
    candidatos: list[str] = []
    api_requests: list[str] = []
    iframes: list[str] = []
    errores: list[str] = []

    def agregar(url):
        if permitido(url) and url not in candidatos:
            candidatos.append(url)

    context = await browser.new_context(
        accept_downloads=False,
        service_workers="block",
        viewport={"width": 1100, "height": 800},
    )

    async def filtrar_ruta(route):
        request = route.request
        url = request.url
        if not permitido(url):
            await route.abort()
            return
        # Impide que un clic provoque una descarga de instaladores.
        if urlparse(url).path.lower().endswith(
            (".exe", ".msi", ".apk", ".dmg", ".bat", ".scr", ".zip")
        ):
            await route.abort()
            return
        await route.continue_()

    await context.route("**/*", filtrar_ruta)
    page = await context.new_page()

    async def cerrar_popup(popup):
        try:
            await popup.close()
        except Exception:
            pass

    page.on("popup", lambda popup: asyncio.create_task(cerrar_popup(popup)))

    def al_solicitar(request):
        u = request.url
        if es_stream(u):
            agregar(u)
        elif request.resource_type in ("xhr", "fetch"):
            if any(k in u.lower() for k in
                   ("video", "play", "episode", "chapter", "stream", "source", "api/")):
                if u not in api_requests:
                    api_requests.append(u)

    async def leer_respuesta(response):
        try:
            if not permitido(response.url) or response.status >= 400:
                return
            mime = response.headers.get("content-type", "").lower()
            if es_stream(response.url) or any(t in mime for t in VIDEO_TYPES):
                agregar(response.url)
            if "json" in mime and response.request.resource_type in ("xhr", "fetch"):
                texto = await response.text()
                if len(texto) < 1_500_000:
                    for u in buscar_en_texto(texto):
                        agregar(u)
        except Exception:
            pass

    page.on("request", al_solicitar)
    page.on("response", lambda response: asyncio.create_task(leer_respuesta(response)))

    try:
        await page.goto(pagina_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3500)
        # Busca recursos de vídeo en los elementos HTML de todos los frames.
        for frame in page.frames:
            if frame != page.main_frame and permitido(frame.url):
                iframes.append(frame.url)
            try:
                recursos = await frame.evaluate("""() => ({
                    media: [...document.querySelectorAll('video, source')].flatMap(x =>
                        [x.currentSrc, x.src, x.getAttribute('src'),
                         x.getAttribute('data-src')]).filter(Boolean),
                    html: document.documentElement.outerHTML.slice(0, 900000)
                })""")
                for u in recursos["media"]:
                    if isinstance(u, str) and permitido(u):
                        agregar(u)
                for u in buscar_en_texto(recursos["html"]):
                    agregar(u)
                # Intenta reproducir vídeos sin pulsar banners o botones publicitarios.
                await frame.evaluate("""() => {
                    for (const v of document.querySelectorAll('video')) {
                        v.muted = true;
                        v.play().catch(() => {});
                    }
                }""")
            except Exception as e:
                errores.append(str(e)[:150])
        await page.wait_for_timeout(4500)
        # Si al reproducir se inserta un <video> dinámico, vuelve a leerlo.
        for frame in page.frames:
            try:
                urls = await frame.evaluate("""() => [...document.querySelectorAll('video,source')]
                    .flatMap(x => [x.currentSrc, x.src]).filter(Boolean)""")
                for u in urls:
                    if permitido(u):
                        agregar(u)
            except Exception:
                pass
    except Exception as e:
        errores.append(str(e)[:300])
    finally:
        await context.close()

    # Prefiere listas HLS (un episodio completo) y MP4 a otros enlaces.
    candidatos.sort(key=lambda u: (0 if es_stream(u) else 1,
                                   0 if ".m3u8" in u.lower() else 1))
    if diagnostico or not candidatos:
        reporte = {
            "capitulo": episodio,
            "pagina": pagina_url,
            "candidatos_video": candidatos,
            "iframes": iframes,
            "solicitudes_api_relacionadas": api_requests[:80],
            "errores": errores,
        }
        (SALIDA / f"diagnostico_{episodio:02d}.json").write_text(
            json.dumps(reporte, indent=2, ensure_ascii=False), encoding="utf-8")
    return candidatos, iframes


async def main():
    parser = argparse.ArgumentParser(description="Extraer capítulos de AskiDrama")
    parser.add_argument("--inicio", type=int, default=1)
    parser.add_argument("--fin", type=int, default=45)
    parser.add_argument("--diagnostico", action="store_true",
                        help="Genera un archivo JSON con lo detectado")
    args = parser.parse_args()
    if args.inicio < 1 or args.fin > 45 or args.inicio > args.fin:
        parser.error("Selecciona capítulos entre 1 y 45, con inicio <= fin")
    SALIDA.mkdir(exist_ok=True)
    correctos = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, args=["--autoplay-policy=no-user-gesture-required"])
        try:
            for numero in range(args.inicio, args.fin + 1):
                if existe_video(numero):
                    print(f"[{numero:02d}] Ya existe, saltando")
                    correctos += 1
                    continue
                pagina_url = f"{BASE}/{numero}"
                print(f"[{numero:02d}] Analizando...", flush=True)
                candidatos, iframes = await inspeccionar(browser, numero, args.diagnostico)
                # URLs de vídeo capturadas + posibles reproductores externos.
                opciones = candidatos + [u for u in iframes if u not in candidatos]
                if not opciones:
                    print(f"[{numero:02d}] No se detectó vídeo. Revisa diagnostico_{numero:02d}.json")
                    continue
                guardado = False
                for url in opciones[:12]:
                    print(f"[{numero:02d}] Probando fuente: {url[:130]}", flush=True)
                    ok, razon = await asyncio.to_thread(
                        descargar_con_ytdlp, url, numero, pagina_url)
                    if ok:
                        print(f"[{numero:02d}] Descargado correctamente")
                        correctos += 1
                        guardado = True
                        break
                    print(f"[{numero:02d}] Fuente fallida: {razon}")
                if not guardado:
                    print(f"[{numero:02d}] No se pudo guardar; prueba --diagnostico")
        finally:
            await browser.close()
    print(f"\nCapítulos disponibles localmente: {correctos} / {args.fin - args.inicio + 1}")
    print(f"Carpeta: {SALIDA.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
