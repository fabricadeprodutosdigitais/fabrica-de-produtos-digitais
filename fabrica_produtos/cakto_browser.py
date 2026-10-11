"""Plano C da capa na Cakto: a API aceita o PUT (200) mas não grava `image` e não tem upload. Então o robô faz o que
você faria no painel: entra com e-mail/senha (conta sem 2FA), abre o produto 'Sem imagem', envia docs/capas/{id}.png
no <input type=file>, salva e CONFIRMA lendo o produto pela API (image != null).

Secrets: CAKTO_EMAIL e CAKTO_PASSWORD (sem eles o módulo sai sem erro). Os seletores do painel não são documentados:
cada passo tem alternativas e, se algo falhar, um screenshot vai para cakto-browser-debug/ (artefato do workflow).
Uso: python -m fabrica_produtos.cakto_browser
"""
import os
import re
import sys

from . import catalogo, config_fabrica as cfg, plataformas

URL_LOGIN = "https://app.cakto.com.br/login"
URL_PRODUTOS = "https://app.cakto.com.br/dashboard/products?tab=products&flow=v2"
PASTA_DEBUG = "cakto-browser-debug"
TEMPO = 30_000  # ms por espera


def pendentes(plat=None) -> list[tuple[dict, str, str]]:
    """[(produto do catálogo, id na Cakto, caminho da capa)] dos produtos que existem na Cakto sem imagem."""
    from .registrador import gerar_capa_produto
    plat = plat or plataformas.Cakto()
    out = []
    for p in catalogo.carregar()["produtos"]:
        if p.get("status") not in ("pronto", "aguardando_cadastro"):
            continue
        achado = plat.buscar_por_nome(p["nome"])
        if not achado or achado.get("imagem"):
            continue
        capa = os.path.join("docs", "capas", f"{p['id']}.png")
        if not os.path.isfile(capa):
            capa = gerar_capa_produto(p, [])
        out.append((p, achado["id"], capa))
    return out


def _primeiro_visivel(page, candidatos, tempo=TEMPO):
    """Primeiro locator da lista que aparece; None se nenhum aparecer."""
    for loc in candidatos:
        try:
            loc.first.wait_for(state="visible", timeout=max(tempo // len(candidatos), 3000))
            return loc.first
        except Exception:
            continue
    return None


CAMPOS_EMAIL = ("input[type=email]", "input[name=email]", "input[name=username]", "input[autocomplete=username]",
                "input[autocomplete=email]", "input[id*=email i]", "input[placeholder*=mail i]",
                "input[type=text]:not([type=password])")


def diagnostico(page, rotulo: str) -> None:
    """Imprime no log o que o robô está vendo (URL, título, campos, texto) e guarda screenshot + HTML no artefato."""
    try:
        campos = page.evaluate("""() => [...document.querySelectorAll('input,button,iframe')].slice(0, 25).map(e =>
            e.tagName.toLowerCase() + '[type=' + (e.type || '') + ',name=' + (e.name || '') + ',id=' + (e.id || '') +
            ',ph=' + (e.placeholder || '') + ',txt=' + (e.innerText || '').slice(0, 30) + ']')""")
        texto = (page.inner_text("body") or "")[:400].replace("\n", " | ")
        print(f"CAKTO_BROWSER_DIAG {rotulo} url={page.url} titulo={page.title()!r} campos={campos} texto={texto!r}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"CAKTO_BROWSER_DIAG {rotulo} indisponivel ({type(e).__name__})", flush=True)
    _debug(page, rotulo)
    try:
        with open(os.path.join(PASTA_DEBUG, f"{rotulo}.html"), "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:  # noqa: BLE001
        pass


def _fechar_avisos(page) -> None:
    """Banner de cookies/LGPD costuma cobrir o formulário."""
    for nome in ("aceitar", "aceito", "concordo", "entendi", "ok", "accept"):
        try:
            b = page.get_by_role("button", name=re.compile(rf"^\s*{nome}", re.I)).first
            if b.is_visible(timeout=500):
                b.click(timeout=1500)
        except Exception:  # noqa: BLE001
            continue


def entrar(page, email: str, senha: str) -> None:
    """O painel é um app React: espera a rede assentar e dá 40 s por variação do campo. Falhou? diagnóstico no log."""
    page.goto(URL_LOGIN, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:  # noqa: BLE001 - pode haver polling eterno; segue assim mesmo
        pass
    _fechar_avisos(page)
    senha_loc = page.locator("input[type=password]")
    try:
        senha_loc.first.wait_for(state="visible", timeout=40_000)
    except Exception:  # noqa: BLE001
        diagnostico(page, "login")
        raise RuntimeError("tela de login não carregou (sem campo de senha); veja CAKTO_BROWSER_DIAG e o artefato")
    campo_email = _primeiro_visivel(page, [page.locator(c) for c in CAMPOS_EMAIL] +
                                    [page.get_by_label(re.compile("e-?mail|usu", re.I))], 21_000)
    if campo_email is None:
        diagnostico(page, "login")
        raise RuntimeError("campo de e-mail não apareceu na tela de login")
    campo_email.click()
    campo_email.fill(email)
    senha_loc.first.fill(senha)
    botao = _primeiro_visivel(page, [page.get_by_role("button", name=re.compile(r"entrar|login|acessar|continuar", re.I)),
                                     page.locator("button[type=submit]")], 10_000)
    if botao is None:
        senha_loc.first.press("Enter")
    else:
        botao.click()
    try:
        page.wait_for_url(lambda u: "/login" not in u, timeout=45_000)
    except Exception:  # noqa: BLE001
        diagnostico(page, "login-apos-enviar")  # senha errada, captcha ou pedido de verificação
        raise RuntimeError("continua na tela de login depois de enviar (credenciais, captcha ou verificação?)")


def abrir_edicao(page, nome: str, cakto_id: str) -> None:
    """Lista de produtos > busca pelo nome > abre o produto > Editar produto (se existir o botão)."""
    page.goto(URL_PRODUTOS, wait_until="domcontentloaded", timeout=TEMPO)
    busca = _primeiro_visivel(page, [page.get_by_placeholder(re.compile(r"buscar|pesquis|search", re.I)),
                                     page.locator("input[type=search]")], 10_000)
    if busca is not None:
        busca.fill(nome[:40])
        page.wait_for_timeout(1500)
    item = _primeiro_visivel(page, [page.get_by_text(nome, exact=False), page.locator(f"a[href*='{cakto_id}']")])
    if item is None:
        page.goto(f"https://app.cakto.com.br/dashboard/products/{cakto_id}", wait_until="domcontentloaded", timeout=TEMPO)
    else:
        item.click()
    editar = _primeiro_visivel(page, [page.get_by_role("button", name=re.compile(r"editar produto", re.I)),
                                      page.get_by_role("link", name=re.compile(r"editar produto", re.I)),
                                      page.get_by_text(re.compile(r"editar produto", re.I))], 10_000)
    if editar is not None:
        editar.click()


def enviar_capa(page, capa: str) -> None:
    entrada = page.locator("input[type=file]")
    entrada.first.wait_for(state="attached", timeout=TEMPO)
    entrada.first.set_input_files(capa)
    page.wait_for_timeout(2000)  # o painel pré-visualiza/envia o arquivo
    salvar = _primeiro_visivel(page, [page.get_by_role("button", name=re.compile(r"^\s*salvar", re.I)),
                                      page.get_by_text(re.compile(r"^\s*salvar", re.I))])
    if salvar is None:
        raise RuntimeError("botão Salvar não encontrado")
    salvar.click()
    page.wait_for_timeout(3000)


def confirmar(plat, cakto_id: str) -> str:
    return plat._req("GET", f"/public_api/products/{cakto_id}/").get("image") or ""


def _debug(page, produto_id: str) -> None:
    try:
        os.makedirs(PASTA_DEBUG, exist_ok=True)
        page.screenshot(path=os.path.join(PASTA_DEBUG, f"{produto_id}.png"), full_page=True)
    except Exception:
        pass


def processar(page, plat, itens) -> dict[str, bool]:
    """Um produto por vez; a falha de um não impede os outros. Cada resultado vira uma linha CAKTO_BROWSER_UPLOAD."""
    res = {}
    for p, cakto_id, capa in itens:
        try:
            abrir_edicao(page, p["nome"], cakto_id)
            enviar_capa(page, capa)
            imagem = confirmar(plat, cakto_id)
            ok = bool(imagem)
            print(f"CAKTO_BROWSER_UPLOAD {p['id']} {'OK' if ok else 'FALHOU: salvou mas a API ainda mostra image=null'}",
                  flush=True)
        except Exception as e:  # noqa: BLE001 - o painel pode mudar de qualquer jeito; registra e segue
            ok = False
            print(f"CAKTO_BROWSER_UPLOAD {p['id']} FALHOU: {type(e).__name__}: {str(e)[:300]}", flush=True)
            _debug(page, p["id"])
        res[p["id"]] = ok
    return res


def main() -> int:
    email, senha = os.getenv("CAKTO_EMAIL", "").strip(), os.getenv("CAKTO_PASSWORD", "")
    if not email or not senha:
        print("CAKTO_BROWSER: CAKTO_EMAIL/CAKTO_PASSWORD não definidos; nada a fazer.")
        return 0
    plat = plataformas.Cakto()
    if not plat.configurada():
        print("CAKTO_BROWSER: faltam CAKTO_CLIENT_ID/CAKTO_CLIENT_SECRET para conferir o resultado; nada a fazer.")
        return 0
    itens = pendentes(plat)
    if not itens:
        print("CAKTO_BROWSER: nenhum produto 'Sem imagem'.")
        return 0
    print(f"CAKTO_BROWSER: {len(itens)} produto(s) sem imagem.")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        navegador = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        page = navegador.new_context(
            viewport={"width": 1366, "height": 900}, locale="pt-BR", timezone_id="America/Sao_Paulo",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36")).new_page()
        try:
            entrar(page, email, senha)
        except Exception as e:  # noqa: BLE001
            print(f"CAKTO_BROWSER_LOGIN FALHOU: {type(e).__name__}: {str(e)[:300]}", flush=True)
            _debug(page, "login")
            navegador.close()
            return 1
        print("CAKTO_BROWSER_LOGIN OK", flush=True)
        res = processar(page, plat, itens)
        navegador.close()
    falhas = [i for i, ok in res.items() if not ok]
    print(f"CAKTO_BROWSER: {len(res) - len(falhas)} ok, {len(falhas)} falha(s).")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
