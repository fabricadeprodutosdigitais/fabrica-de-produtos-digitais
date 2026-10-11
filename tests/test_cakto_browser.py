"""Plano C da capa: upload pelo painel da Cakto com Playwright (aqui sem navegador: páginas falsas)."""
import os
import tempfile
import unittest
from unittest import mock

from tests import stubs
stubs.instalar()

from fabrica_produtos import cakto_browser as cb, catalogo, config_fabrica as cfg, plataformas  # noqa: E402
from tests.fixtures import produto_bom  # noqa: E402
from tests.test_cakto import Resp, cliente, produto_api  # noqa: E402

RAIZ = os.path.join(os.path.dirname(__file__), "..")
NOME = produto_bom()["nome"]


class Base(unittest.TestCase):
    def setUp(self):
        for k in ("CAKTO_CLIENT_ID", "CAKTO_CLIENT_SECRET", "CAKTO_EMAIL", "CAKTO_PASSWORD"):
            self.addCleanup(os.environ.pop, k, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg.CATALOGO_PATH = os.path.join(self.tmp.name, "catalogo.json")
        r = catalogo.adicionar(produto_bom(), "aprovado", "x")
        catalogo.atualizar(r["id"], "ok", status="pronto", link_compra="https://pay.cakto.com.br/abc", plataforma="cakto")
        self.pid = r["id"]

    def api(self, imagem):
        sem = produto_api(NOME, pid="u1")
        lista = Resp(200, {"results": [sem]})
        c, srv = cliente({("GET", "/public_api/products/"): lista,
                          ("GET", "/public_api/products/u1/"): Resp(200, {**sem, "image": imagem})})
        p = mock.patch.object(plataformas.requests, "request", srv)
        p.start()
        self.addCleanup(p.stop)
        return c


class Pendentes(Base):
    def test_so_lista_produto_sem_imagem_e_gera_a_capa_se_faltar(self):
        c = self.api(None)
        with mock.patch("fabrica_produtos.registrador.gerar_capa_produto", return_value="docs/capas/x.png"):
            itens = cb.pendentes(c)
        self.assertEqual([(i[0]["id"], i[1]) for i in itens], [(self.pid, "u1")])

    def test_produto_com_imagem_e_pulado(self):
        c = self.api("https://cdn/x.png")
        self.assertEqual(cb.pendentes(c), [])


class Processar(Base):
    def test_ok_so_quando_a_api_confirma_image(self):
        c = self.api("https://cdn/x.png")
        item = (catalogo.obter(self.pid), "u1", "docs/capas/x.png")
        with mock.patch.object(cb, "abrir_edicao"), mock.patch.object(cb, "enviar_capa"), \
                mock.patch("builtins.print") as pr:
            res = cb.processar(mock.Mock(), c, [item])
        self.assertEqual(res, {self.pid: True})
        self.assertIn(f"CAKTO_BROWSER_UPLOAD {self.pid} OK", pr.call_args_list[0].args[0])

    def test_salvou_mas_image_null_conta_como_falha(self):
        c = self.api(None)
        item = (catalogo.obter(self.pid), "u1", "docs/capas/x.png")
        with mock.patch.object(cb, "abrir_edicao"), mock.patch.object(cb, "enviar_capa"), mock.patch("builtins.print"):
            self.assertEqual(cb.processar(mock.Mock(), c, [item]), {self.pid: False})

    def test_erro_em_um_produto_nao_derruba_os_outros(self):
        c = self.api("https://cdn/x.png")
        item = (catalogo.obter(self.pid), "u1", "x.png")
        chamadas = []

        def abrir(page, nome, cid):
            chamadas.append(nome)
            if len(chamadas) == 1:
                raise RuntimeError("painel mudou")
        with mock.patch.object(cb, "abrir_edicao", abrir), mock.patch.object(cb, "enviar_capa"), \
                mock.patch.object(cb, "_debug") as dbg, mock.patch("builtins.print"):
            res = cb.processar(mock.Mock(), c, [item, item])
        self.assertEqual(list(res.values()), [True])  # mesmo id: o 2º sobrescreve o 1º; o 1º falhou e foi depurado
        dbg.assert_called_once()

    def test_enviar_capa_usa_input_file_e_clica_salvar(self):
        page = mock.MagicMock()
        with mock.patch.object(cb, "_primeiro_visivel", return_value=mock.Mock()) as pv:
            cb.enviar_capa(page, "docs/capas/x.png")
        page.locator.assert_called_with("input[type=file]")
        page.locator.return_value.first.set_input_files.assert_called_with("docs/capas/x.png")
        pv.return_value.click.assert_called_once()


class Main(Base):
    def test_sem_email_ou_senha_sai_sem_erro(self):
        with mock.patch("builtins.print"):
            self.assertEqual(cb.main(), 0)

    def test_sem_pendentes_nao_abre_navegador(self):
        os.environ.update(CAKTO_EMAIL="a@b.c", CAKTO_PASSWORD="s", CAKTO_CLIENT_ID="i", CAKTO_CLIENT_SECRET="s")
        with mock.patch.object(cb, "pendentes", return_value=[]), mock.patch("builtins.print"):
            self.assertEqual(cb.main(), 0)


LOGIN_HTML = """<html><body><div id=app></div><script>
setTimeout(()=>{document.getElementById('app').innerHTML =
 '<input name=login placeholder="Seu e-mail"><input type=password><button type=submit>Entrar</button>';
 document.querySelector('button').onclick=()=>{location.href='%s#/painel'};}, 1200);
</script></body></html>"""


class LoginNavegadorReal(unittest.TestCase):
    """Chromium de verdade contra uma página local que monta o formulário tarde (como um app React)."""

    def test_login_em_pagina_que_renderiza_depois(self):
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            nav = pw.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"sem Chromium: {e}")
        self.addCleanup(pw.stop)
        self.addCleanup(nav.close)
        import http.server
        import threading
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "login.html"), "w") as f:
            f.write("")  # preenchido abaixo, quando a porta é conhecida
        with open(os.path.join(d, "ok.html"), "w") as f:
            f.write("<html>ok</html>")

        class H(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=d, **k)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_port}"
        with open(os.path.join(d, "login.html"), "w") as f:
            f.write(LOGIN_HTML % f"{base}/ok.html")
        page = nav.new_page()
        # 1ª URL dá 404 (como o /login real), a 2ª é a tela de login que monta o formulário com atraso
        with mock.patch.object(cb, "URLS_ENTRADA", (f"{base}/nao-existe.html", f"{base}/login.html")):
            cb.entrar(page, "a@b.c", "senha")
        self.assertIn("ok.html", page.url)

    def test_falha_gera_diagnostico_no_log(self):
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            nav = pw.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"sem Chromium: {e}")
        self.addCleanup(pw.stop)
        self.addCleanup(nav.close)
        page = nav.new_page()
        page.set_content("<html><title>Just a moment...</title><body>Verificando se voce e humano</body></html>")
        cwd = os.getcwd()
        os.chdir(tempfile.mkdtemp())
        self.addCleanup(os.chdir, cwd)
        with mock.patch("builtins.print") as pr:
            cb.diagnostico(page, "login")
        saida = pr.call_args_list[0].args[0]
        self.assertIn("CAKTO_BROWSER_DIAG", saida)
        self.assertIn("Just a moment", saida)
        self.assertTrue(os.path.isfile(os.path.join(cb.PASTA_DEBUG, "login.html")))


class DiagSSO(unittest.TestCase):
    def test_linha_sso_mostra_url_titulo_e_campos(self):
        page = mock.MagicMock()
        page.url, page.title.return_value = cb.URL_SSO, "Entrar | Cakto"
        page.locator.return_value.count.return_value = 1
        with mock.patch("builtins.print") as pr:
            cb._log_sso(page)
        linha = pr.call_args_list[0].args[0]
        for trecho in ("CAKTO_BROWSER_DIAG sso_url=https://sso.cakto.com.br", "titulo='Entrar | Cakto'",
                       "tem_email=True", "tem_senha=True"):
            self.assertIn(trecho, linha)

    def test_campos_cobrem_name_email_type_email_e_login(self):
        for c in ("input[name=email]", "input[type=email]", "input[name=login]"):
            self.assertIn(c, cb.CAMPOS_EMAIL)


class Workflow(unittest.TestCase):
    def test_job_cakto_browser_depois_do_fabrica_e_so_com_email(self):
        with open(os.path.join(RAIZ, ".github", "workflows", "produto.yml"), encoding="utf-8") as f:
            txt = f.read()
        j = txt[txt.index("  cakto-browser:"):]
        self.assertIn("needs: fabrica", j)
        self.assertIn("CAKTO_EMAIL: ${{ secrets.CAKTO_EMAIL }}", j)
        self.assertIn("env.CAKTO_EMAIL != ''", j)
        self.assertIn("playwright install", j)
        self.assertIn("python -m fabrica_produtos.cakto_browser", j)
        self.assertIn("CAKTO_EXIGE_IMAGEM: ${{ vars.CAKTO_EXIGE_IMAGEM || 'false' }}", j)

    def test_entrada_comeca_pelo_painel_que_redireciona_para_o_login_real(self):
        self.assertEqual(cb.URLS_ENTRADA[0], cb.URL_SSO)
        self.assertTrue(cb.URL_SSO.startswith("https://sso.cakto.com.br/accounts/login/?next="))
        self.assertIn("dashboard%2Fhome", cb.URL_SSO)
        self.assertIn("dashboard/products", cb.URLS_ENTRADA[1])
        self.assertIn(cb.URL_LOGIN, cb.URLS_ENTRADA)   # o /login que deu 404 fica só como último palpite
        self.assertIn("dashboard/products?tab=products&flow=v2", cb.URL_PRODUTOS)


if __name__ == "__main__":
    unittest.main()
