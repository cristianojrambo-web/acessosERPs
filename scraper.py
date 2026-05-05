"""
Scraper para o ERP Teleport (teleport.com.br).
Usa Playwright para fazer login e extrair dados via navegação web.

Configuração necessária no arquivo .env:
    TELEPORT_URL=https://teleport.com.br/0
    TELEPORT_USERNAME=seu_usuario
    TELEPORT_PASSWORD=sua_senha

Após instalar: playwright install chromium
"""

import os
import logging
from dataclasses import dataclass, field
from typing import Callable
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TELEPORT_URL = os.getenv("TELEPORT_URL", "https://teleport.com.br/0")
TELEPORT_USERNAME = os.getenv("TELEPORT_USERNAME", "")
TELEPORT_PASSWORD = os.getenv("TELEPORT_PASSWORD", "")

# Section definitions: key → (display label, menu keywords for navigation)
SECTIONS: dict[str, dict] = {
    "clientes": {
        "label": "Clientes",
        "keywords": ["cliente", "segurado", "tomador"],
    },
    "apolices": {
        "label": "Apólices",
        "keywords": ["apólice", "apolice", "proposta"],
    },
    "sinistros": {
        "label": "Sinistros",
        "keywords": ["sinistro", "aviso"],
    },
    "financeiro": {
        "label": "Financeiro",
        "keywords": ["financeiro", "comissão", "comissao", "repasse", "extrato"],
    },
}


@dataclass
class ScraperResult:
    success: bool = False
    dataframes: dict[str, pd.DataFrame] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    message: str = ""


# ── DOM Helpers ────────────────────────────────────────────────────────────────

def _safe_wait(page, timeout: int = 5_000) -> None:
    """Waits for network idle, swallowing navigation-related errors."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass
    page.wait_for_timeout(1_000)


def _find_and_click_menu(page, keywords: list[str]) -> bool:
    """Clicks the first visible nav link that matches any keyword."""
    for kw in keywords:
        candidates = [
            f"nav a:text-matches('{kw}', 'i')",
            f"[role='navigation'] a:text-matches('{kw}', 'i')",
            f"[class*='menu'] a:text-matches('{kw}', 'i')",
            f"[class*='sidebar'] a:text-matches('{kw}', 'i')",
            f"[class*='nav'] a:text-matches('{kw}', 'i')",
            f"a:text-matches('{kw}', 'i')",
            f"button:text-matches('{kw}', 'i')",
        ]
        for sel in candidates:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=600):
                    # Use expect_navigation to safely handle page transitions
                    try:
                        with page.expect_navigation(wait_until="domcontentloaded", timeout=8_000):
                            el.click()
                    except Exception:
                        page.wait_for_timeout(1_500)
                    return True
            except Exception:
                continue
    return False


def _extract_html_tables(page) -> list[pd.DataFrame]:
    """Extracts DataFrames from all <table> and role=grid elements on the page."""
    frames: list[pd.DataFrame] = []

    # ── Standard HTML <table> ─────────────────────────────────────────────────
    for tbl in page.query_selector_all("table"):
        try:
            head_els = tbl.query_selector_all("thead tr th, thead tr td")
            if not head_els:
                head_els = tbl.query_selector_all("tr:first-child th")
            if not head_els:
                head_els = tbl.query_selector_all("tr:first-child td")
            headers = [h.inner_text().strip() for h in head_els]

            body_rows = tbl.query_selector_all("tbody tr")
            if not body_rows:
                all_rows = tbl.query_selector_all("tr")
                body_rows = all_rows[1:] if len(all_rows) > 1 else []

            data = []
            for row in body_rows:
                cells = row.query_selector_all("td")
                if cells:
                    data.append([c.inner_text().strip() for c in cells])

            if not data:
                continue

            n = len(data[0])
            if len(headers) > n:
                headers = headers[:n]
            elif len(headers) < n:
                headers += [f"col_{i}" for i in range(len(headers), n)]

            df = pd.DataFrame(data, columns=headers)
            if len(df) > 0:
                frames.append(df)
        except Exception as exc:
            logger.debug("Table extraction skipped: %s", exc)

    # ── ARIA grid (ag-Grid, Kendo, Angular Material, etc.) ────────────────────
    if not frames:
        for grid in page.query_selector_all("[role='grid'], [class*='ag-root'], [class*='k-grid']"):
            try:
                headers = [
                    h.inner_text().strip()
                    for h in grid.query_selector_all("[role='columnheader']")
                ]
                rows_els = grid.query_selector_all("[role='row']")
                data = []
                for row in rows_els:
                    cells = row.query_selector_all("[role='gridcell']")
                    if cells:
                        data.append([c.inner_text().strip() for c in cells])

                if data and headers:
                    df = pd.DataFrame(data, columns=headers[: len(data[0])])
                    if len(df) > 0:
                        frames.append(df)
            except Exception:
                continue

    return frames


def _df_from_json_responses(captured: list[dict]) -> pd.DataFrame | None:
    """Tries to build a DataFrame from captured JSON API responses."""
    best: pd.DataFrame | None = None

    for entry in captured:
        body = entry.get("data")
        try:
            if isinstance(body, list) and len(body) > 0 and isinstance(body[0], dict):
                df = pd.json_normalize(body)
                if best is None or len(df) > len(best):
                    best = df
            elif isinstance(body, dict):
                for val in body.values():
                    if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                        df = pd.json_normalize(val)
                        if best is None or len(df) > len(best):
                            best = df
        except Exception:
            continue

    return best


# ── Public API ─────────────────────────────────────────────────────────────────

def _run_playwright(
    username: str,
    password: str,
    base_url: str,
    to_scrape: list[str],
    headless: bool,
    max_rows: int,
    progress_callback: Callable[[str, float], None] | None,
) -> ScraperResult:
    """
    Executa o Playwright em uma thread isolada.
    Configura WindowsSelectorEventLoopPolicy no Windows para evitar conflito
    com o event loop do Streamlit (NotImplementedError no asyncio).
    """
    import sys
    import asyncio
    import json
    from pathlib import Path

    if sys.platform == "win32":
        # ProactorEventLoop é obrigatório no Windows para criar subprocessos
        # (o SelectorEventLoop não suporta create_subprocess_exec)
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        loop = asyncio.ProactorEventLoop()
        asyncio.set_event_loop(loop)

    from playwright.sync_api import sync_playwright

    result = ScraperResult()

    def report(msg: str, pct: float) -> None:
        logger.info(msg)
        if progress_callback:
            progress_callback(msg, min(pct, 1.0))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        ctx = browser.new_context(viewport={"width": 1366, "height": 768})
        page = ctx.new_page()

        try:
            debug_dir = Path(__file__).parent

            # ── Login ──────────────────────────────────────────────────────────
            report("Conectando ao Teleport ERP…", 0.04)
            page.goto(base_url, wait_until="domcontentloaded", timeout=20_000)
            page.wait_for_timeout(2_000)

            # Screenshot da tela de login para diagnóstico
            try:
                page.screenshot(path=str(debug_dir / "debug_1_login_page.png"))
            except Exception:
                pass

            report("Preenchendo credenciais…", 0.08)

            user_selectors = [
                "input[type='email']",
                "input[name='email']",
                "input[name='login']",
                "input[name='username']",
                "input[id='email']",
                "input[id='login']",
                "input[id='username']",
                "input[placeholder*='mail']",
                "input[placeholder*='suário']",
                "input[placeholder*='ogin']",
                "input[type='text']:visible",
            ]
            filled_user = False
            for sel in user_selectors:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=800):
                        el.fill(username)
                        filled_user = True
                        break
                except Exception:
                    continue

            if not filled_user:
                try:
                    page.screenshot(path=str(debug_dir / "debug_1_login_page.png"))
                except Exception:
                    pass
                result.message = "Campo de usuário não encontrado na página de login."
                return result

            try:
                pw_field = page.locator("input[type='password']").first
                pw_field.wait_for(state="visible", timeout=5_000)
                pw_field.fill(password)
            except Exception:
                result.message = "Campo de senha não encontrado na página de login."
                return result

            submit_selectors = [
                "button[type='submit']",
                "input[type='submit']",
                "button:text-matches('entrar', 'i')",
                "button:text-matches('login', 'i')",
                "button:text-matches('acessar', 'i')",
                "button:text-matches('confirmar', 'i')",
            ]
            submitted = False
            for sel in submit_selectors:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=500):
                        btn.click()
                        submitted = True
                        break
                except Exception:
                    continue

            if not submitted:
                page.locator("input[type='password']").first.press("Enter")

            # Aguarda a página pós-login carregar (sem depender de URL mudar)
            page.wait_for_timeout(3_000)
            _safe_wait(page, timeout=8_000)

            # Detect login failure — wrap in try/except in case context changes
            try:
                error_els = page.query_selector_all(
                    "[class*='error'], [class*='alert-danger'], "
                    "[class*='invalid-feedback'], .error, #error"
                )
                visible_errors = [e for e in error_els if e.is_visible()]
                if visible_errors:
                    msg = visible_errors[0].inner_text().strip()
                    result.message = f"Erro de login: {msg}" if msg else "Credenciais inválidas."
                    return result
            except Exception:
                pass

            try:
                body_text = page.inner_text("body").lower()
                failure_phrases = [
                    "senha incorreta", "usuário não encontrado",
                    "invalid credentials", "inválido", "não autorizado",
                ]
                if any(ph in body_text for ph in failure_phrases):
                    result.message = "Credenciais inválidas. Verifique TELEPORT_USERNAME e TELEPORT_PASSWORD."
                    return result
            except Exception:
                pass

            report("Login realizado com sucesso!", 0.15)
            result.success = True

            # ── Debug: captura estrutura de navegação após login ───────────────
            try:
                debug_dir = Path(__file__).parent
                page.screenshot(path=str(debug_dir / "debug_login.png"))

                all_links = page.eval_on_selector_all(
                    "a",
                    "els => els.map(el => ({text: el.innerText.trim(), href: el.href})).filter(l => l.text.length > 0)"
                )
                all_buttons = page.eval_on_selector_all(
                    "button, [role='button'], [class*='menu-item'], [class*='nav-item']",
                    "els => els.map(el => el.innerText.trim()).filter(t => t.length > 0)"
                )
                debug_info = {
                    "url_pos_login": page.url,
                    "titulo": page.title(),
                    "links": all_links[:40],
                    "botoes_e_menu": list(set(all_buttons))[:40],
                }
                with open(str(debug_dir / "debug_nav.json"), "w", encoding="utf-8") as _f:
                    json.dump(debug_info, _f, ensure_ascii=False, indent=2)
                report(f"Debug salvo em debug_login.png e debug_nav.json (URL: {page.url})", 0.16)
            except Exception as _de:
                logger.debug("Debug capture error: %s", _de)

            # ── Scrape each section ────────────────────────────────────────────
            n = len(to_scrape)
            for i, key in enumerate(to_scrape):
                sec = SECTIONS[key]
                p0 = 0.15 + (i / n) * 0.82
                p1 = 0.15 + ((i + 1) / n) * 0.82

                report(f"Navegando para {sec['label']}…", p0)

                # Intercept JSON responses during navigation
                captured_json: list[dict] = []

                def _on_response(resp, _cap=captured_json):
                    try:
                        ct = resp.headers.get("content-type", "")
                        if resp.status == 200 and "json" in ct:
                            _cap.append({"data": resp.json(), "url": resp.url})
                    except Exception:
                        pass

                page.on("response", _on_response)

                # Try menu click first
                navigated = _find_and_click_menu(page, sec["keywords"])

                # Fallback: direct URL guesses (singular, plural, hash routing)
                if not navigated:
                    for kw in sec["keywords"]:
                        kw_plural = kw + "s" if not kw.endswith("s") else kw
                        for candidate in (
                            f"{base_url}/{kw}",
                            f"{base_url}/{kw_plural}",
                            f"{base_url}/#/{kw}",
                            f"{base_url}/#/{kw_plural}",
                            f"{base_url}/#!/{kw}",
                            f"{base_url}/#!/{kw_plural}",
                        ):
                            try:
                                page.goto(candidate, wait_until="domcontentloaded", timeout=8_000)
                                page.wait_for_timeout(1_500)
                                if kw in page.url.lower() or kw_plural in page.url.lower():
                                    navigated = True
                                    break
                            except Exception:
                                continue
                        if navigated:
                            break

                _safe_wait(page, timeout=5_000)
                try:
                    page.remove_listener("response", _on_response)
                except Exception:
                    pass

                if not navigated:
                    result.errors[key] = f"Não foi possível navegar para {sec['label']}"
                    report(f"  ⚠ {sec['label']}: seção não encontrada", p1)
                    continue

                # Trigger search/list to populate data if needed
                for btn_text in ["Buscar", "Pesquisar", "Listar", "Ver todos", "Todos"]:
                    try:
                        btn = page.locator(f"button:text-matches('{btn_text}', 'i')").first
                        if btn.is_visible(timeout=600):
                            with page.expect_navigation(wait_until="networkidle", timeout=10_000):
                                btn.click()
                            break
                    except Exception:
                        pass

                report(f"Extraindo dados de {sec['label']}…", (p0 + p1) / 2)

                # 1. Try JSON API data (richer, cleaner)
                df = _df_from_json_responses(captured_json)

                # 2. Fallback to HTML scraping
                if df is None or len(df) == 0:
                    tables = _extract_html_tables(page)
                    if tables:
                        df = max(tables, key=len)

                if df is not None and len(df) > 0:
                    result.dataframes[sec["label"]] = df.head(max_rows)
                    report(f"  ✓ {sec['label']}: {len(df)} registros", p1)
                else:
                    result.errors[key] = f"Nenhum dado encontrado em {sec['label']}"
                    report(f"  ⚠ {sec['label']}: dados não encontrados", p1)

            report("Importação concluída!", 1.0)

        except Exception as exc:
            result.message = f"Erro durante a importação: {exc}"
            logger.exception("Scraping error")
        finally:
            ctx.close()
            browser.close()

    return result


def scrape_teleport(
    headless: bool = True,
    sections: list[str] | None = None,
    progress_callback: Callable[[str, float], None] | None = None,
    max_rows: int = 500,
) -> ScraperResult:
    """
    Faz login no Teleport ERP e extrai dados das seções indicadas.
    Executa o Playwright em subprocesso separado para evitar conflito de
    event loop asyncio com o Streamlit no Windows (NotImplementedError).
    """
    import sys
    import subprocess
    import tempfile
    import json
    import os
    from pathlib import Path

    if not TELEPORT_USERNAME or not TELEPORT_PASSWORD:
        return ScraperResult(
            success=False,
            message=(
                "Credenciais não configuradas. "
                "Adicione TELEPORT_USERNAME e TELEPORT_PASSWORD ao arquivo .env"
            ),
        )

    to_scrape = [s for s in (sections or list(SECTIONS.keys())) if s in SECTIONS]

    # Write result to a temp file so the subprocess can pass data back
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    output_file = tmp.name

    script = str(Path(__file__).resolve())
    cmd = [
        sys.executable, script,
        "--worker", output_file,
        "--max-rows", str(max_rows),
    ]
    if headless:
        cmd.append("--headless")
    cmd += to_scrape

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=90,
            cwd=str(Path(__file__).parent),
        )

        stderr_output = proc.stderr.strip()

        # Parse progress lines from stdout (for logging)
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                if progress_callback and "msg" in msg:
                    progress_callback(msg["msg"], float(msg.get("pct", 0)))
            except Exception:
                pass

        if not os.path.exists(output_file) or os.path.getsize(output_file) == 0:
            detail = stderr_output or proc.stdout.strip() or "Worker encerrou sem gravar resultado."
            return ScraperResult(success=False, message=f"Falha no worker:\n{detail}")

        with open(output_file, encoding="utf-8") as f:
            data = json.load(f)

        result = ScraperResult(
            success=data.get("success", False),
            message=data.get("message", ""),
            errors=data.get("errors", {}),
        )
        for name, records in data.get("dataframes", {}).items():
            result.dataframes[name] = pd.DataFrame(records)

        return result

    except subprocess.TimeoutExpired as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.output or "").strip()
        detail = stderr or stdout or "sem saída"
        return ScraperResult(success=False, message=f"Timeout (90s).\nÚltima saída:\n{detail}")
    except FileNotFoundError as exc:
        return ScraperResult(success=False, message=f"Arquivo de resultado não encontrado: {exc}")
    except Exception as exc:
        return ScraperResult(success=False, message=f"Erro ao executar importação: {type(exc).__name__}: {exc}")
    finally:
        try:
            os.unlink(output_file)
        except Exception:
            pass


# ── Worker entry point (called as subprocess) ──────────────────────────────────

if __name__ == "__main__":
    import sys
    import json
    import argparse
    import datetime
    from pathlib import Path as _Path

    _log_path = _Path(__file__).parent / "debug_scraper.log"

    def _log(msg: str) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        try:
            with open(_log_path, "a", encoding="utf-8") as _lf:
                _lf.write(line)
        except Exception:
            pass

    _log("=== worker iniciado ===")
    _log(f"Python: {sys.version}")

    # Garante ProactorEventLoop antes de qualquer importação do Playwright
    if sys.platform == "win32":
        import asyncio
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        asyncio.set_event_loop(asyncio.ProactorEventLoop())
        _log("ProactorEventLoop configurado")

    _log("importando playwright...")
    try:
        from playwright.sync_api import sync_playwright as _check_pw  # noqa
        _log("playwright importado ok")
    except Exception as _e:
        _log(f"ERRO ao importar playwright: {_e}")

    parser = argparse.ArgumentParser(description="Teleport scraper worker")
    parser.add_argument("--worker", required=True, metavar="OUTPUT_FILE",
                        help="Path to JSON output file")
    parser.add_argument("--headless", action="store_true", default=False)
    parser.add_argument("--max-rows", type=int, default=500)
    parser.add_argument("sections", nargs="*", default=list(SECTIONS.keys()))
    args = parser.parse_args()
    _log(f"args: headless={args.headless} sections={args.sections}")

    def _progress(msg: str, pct: float) -> None:
        _log(f"  progresso: {msg} ({pct:.0%})")
        print(json.dumps({"msg": msg, "pct": pct}), flush=True)

    _log("chamando _run_playwright...")
    res = _run_playwright(
        username=TELEPORT_USERNAME,
        password=TELEPORT_PASSWORD,
        base_url=TELEPORT_URL.rstrip("/"),
        to_scrape=[s for s in args.sections if s in SECTIONS],
        headless=args.headless,
        max_rows=args.max_rows,
        progress_callback=_progress,
    )

    output = {
        "success": res.success,
        "message": res.message,
        "errors": res.errors,
        "dataframes": {
            name: df.to_dict(orient="records")
            for name, df in res.dataframes.items()
        },
    }

    _log(f"_run_playwright concluído: success={res.success} msg={res.message[:80]}")

    with open(args.worker, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, default=str)

    _log("resultado gravado. encerrando.")
    sys.exit(0 if res.success else 1)
