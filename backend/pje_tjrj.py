"""
LexFlow - Login Selenium no PJE 1G TJRJ
Abre Chrome headless, faz login com CPF+senha em tjrj.pje.jus.br/1g/loginOld.seam,
e busca dados completos de um processo por CNJ.

Uso:
    from pje_tjrj import PJE_TJRJ
    client = PJE_TJRJ()
    ok = client.login(cpf, senha)        # retorna (True/False, msg)
    data = client.fetch_processo(cnj)    # retorna dict com partes, movs, etc
    client.close()

Se nao houver SELENIUM_OK (sem Chrome/chromedriver), login() retorna False
e fetch_processo() retorna {"ok": False, "error": "..."}.
"""
import os
import re
import time
import threading
import logging

log = logging.getLogger("pje_tjrj")

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import (
        TimeoutException, NoSuchElementException, WebDriverException,
    )
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        HAS_WDM = True
    except Exception:
        HAS_WDM = False
    SELENIUM_OK = True
except Exception as _e:
    log.warning("[pje_tjrj] Selenium indisponivel: %s", _e)
    SELENIUM_OK = False

PJE_TJRJ_LOGIN_URL = "https://tjrj.pje.jus.br/1g/loginOld.seam"
PJE_TJRJ_SEARCH_URL = "https://tjrj.pje.jus.br/1g/Processo/ConsultaProcesso/listView.seam"
PAGE_LOAD_TIMEOUT = 20
LOGIN_TIMEOUT = 25
FETCH_TIMEOUT = 30


def _normalize_cnj(cnj):
    """Aceita CNJ formatado ou so digitos, retorna tupla (formatado, digitos)."""
    digits = re.sub(r"\D", "", cnj or "")
    if len(digits) != 20:
        return None, None
    formatted = f"{digits[0:7]}-{digits[7:9]}.{digits[9:13]}.{digits[13]}.{digits[14:16]}.{digits[16:20]}"
    return formatted, digits


class PJE_TJRJ:
    """Cliente Selenium para o PJE 1G TJRJ. Thread-unsafe - uma instancia por aba."""

    def __init__(self, headless=True):
        self.driver = None
        self.headless = headless
        self._logged_in = False
        self._lock = threading.Lock()

    def _build_driver(self):
        if not SELENIUM_OK:
            raise RuntimeError("Selenium nao disponivel no sistema")
        opts = Options()
        if self.headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--window-size=1280,800")
        opts.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        if HAS_WDM:
            service = Service(ChromeDriverManager().install())
        else:
            service = Service()
        driver = webdriver.Chrome(service=service, options=opts)
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        try:
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
            )
        except Exception:
            pass
        return driver

    def _ensure_driver(self):
        if self.driver is None:
            self.driver = self._build_driver()
        return self.driver

    def _has_captcha(self, driver):
        """Detecta captchas comuns do PJE."""
        for sel in ["iframe[src*='recaptcha']", ".g-recaptcha", "img[src*='captcha']", "#captcha"]:
            try:
                if driver.find_elements(By.CSS_SELECTOR, sel):
                    return True
            except Exception:
                pass
        return False

    def login(self, cpf, senha, timeout=LOGIN_TIMEOUT):
        """Faz login no PJE 1G TJRJ. Retorna (ok, mensagem)."""
        if not SELENIUM_OK:
            return False, "Selenium indisponivel neste sistema"
        if not cpf or not senha:
            return False, "CPF e senha sao obrigatorios"
        with self._lock:
            try:
                driver = self._ensure_driver()
                driver.get(PJE_TJRJ_LOGIN_URL)
            except (TimeoutException, WebDriverException) as e:
                return False, f"Falha ao abrir pagina de login: {e}"
            except Exception as e:
                return False, f"Erro inesperado: {e}"

            cpf_selectors = [
                (By.ID, "login:cpfCnpj"),
                (By.ID, "login:username"),
                (By.NAME, "login:cpfCnpj"),
                (By.NAME, "login:username"),
                (By.CSS_SELECTOR, "input[id$='cpfCnpj']"),
                (By.CSS_SELECTOR, "input[id$='username']"),
                (By.CSS_SELECTOR, "input[placeholder*='CPF' i]"),
                (By.CSS_SELECTOR, "input[placeholder*='CNPJ' i]"),
                (By.CSS_SELECTOR, "input[type='text']:first-of-type"),
            ]
            senha_selectors = [
                (By.ID, "login:senha"),
                (By.ID, "login:password"),
                (By.NAME, "login:senha"),
                (By.NAME, "login:password"),
                (By.CSS_SELECTOR, "input[id$='senha']"),
                (By.CSS_SELECTOR, "input[id$='password']"),
                (By.CSS_SELECTOR, "input[type='password']"),
            ]
            submit_selectors = [
                (By.ID, "login:entrar"),
                (By.ID, "login:botaoEntrar"),
                (By.NAME, "login:entrar"),
                (By.CSS_SELECTOR, "input[id$='entrar']"),
                (By.CSS_SELECTOR, "input[value='Entrar']"),
                (By.CSS_SELECTOR, "button[type='submit']"),
            ]
            try:
                wait = WebDriverWait(driver, timeout)
                cpf_input = None
                for by, sel in cpf_selectors:
                    try:
                        cpf_input = wait.until(EC.presence_of_element_located((by, sel)))
                        if cpf_input:
                            break
                    except TimeoutException:
                        continue
                if not cpf_input:
                    if self._has_captcha(driver):
                        return False, "Captcha detectado. Acesse o PJE manualmente pelo Chrome 1 vez para liberar."
                    return False, "Campo de CPF/CNPJ nao encontrado. O PJE pode ter mudado o layout."
                cpf_input.clear()
                cpf_input.send_keys(re.sub(r"\D", "", cpf))
                senha_input = None
                for by, sel in senha_selectors:
                    try:
                        senha_input = driver.find_element(by, sel)
                        break
                    except NoSuchElementException:
                        continue
                if not senha_input:
                    return False, "Campo de senha nao encontrado."
                senha_input.clear()
                senha_input.send_keys(senha)
                submitted = False
                for by, sel in submit_selectors:
                    try:
                        btn = driver.find_element(by, sel)
                        btn.click()
                        submitted = True
                        break
                    except NoSuchElementException:
                        continue
                if not submitted:
                    senha_input.send_keys(Keys.RETURN)
                time.sleep(2)
                current_url = driver.current_url.lower()
                if "login" not in current_url and "autenticar" not in current_url:
                    self._logged_in = True
                    return True, "Login efetuado com sucesso"
                try:
                    msg = driver.find_element(By.CSS_SELECTOR, ".mensagem, .erro, .alert, .ui-messages-error").text
                    return False, f"Login falhou: {msg.strip()[:200]}"
                except NoSuchElementException:
                    return False, "Login falhou. Verifique CPF e senha."
            except TimeoutException:
                return False, "Timeout ao tentar login. O PJE pode estar fora do ar."
            except Exception as e:
                return False, f"Erro no login: {e}"

    def fetch_processo(self, cnj, timeout=FETCH_TIMEOUT):
        """Busca dados completos de um processo no PJE 1G TJRJ."""
        if not SELENIUM_OK:
            return {"ok": False, "data": None, "error": "Selenium indisponivel"}
        formatted, digits = _normalize_cnj(cnj)
        if not formatted:
            return {"ok": False, "data": None, "error": "CNJ invalido (precisa 20 digitos)"}
        if not self._logged_in:
            return {"ok": False, "data": None, "error": "Faca login antes de buscar processo"}

        with self._lock:
            try:
                driver = self._ensure_driver()
                search_url = f"{PJE_TJRJ_SEARCH_URL}?numero={digits}"
                driver.get(search_url)
                WebDriverWait(driver, timeout).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
                time.sleep(1)
                html = driver.page_source
                data = {
                    "cnj": formatted,
                    "url": driver.current_url,
                    "raw_html_len": len(html),
                    "classe": self._extract_text(html, ["Classe:", "Classe Processual:"]),
                    "assunto": self._extract_text(html, ["Assunto:"]),
                    "orgao": self._extract_text(html, ["Orgao Julgador:", "Orgao:"]),
                    "situacao": self._extract_text(html, ["Situacao:"]),
                    "valor_causa": self._extract_text(html, ["Valor da Causa:"]),
                    "partes": self._extract_partes(html),
                }
                return {"ok": True, "data": data, "error": None}
            except TimeoutException:
                return {"ok": False, "data": None, "error": "Timeout ao buscar processo"}
            except Exception as e:
                return {"ok": False, "data": None, "error": f"Erro: {e}"}

    def _extract_text(self, html, labels):
        """Extrai o texto que vem depois de um label."""
        for label in labels:
            m = re.search(re.escape(label) + r"\s*([^<\n]+?)(?:\s*<|\n|$)", html, re.IGNORECASE)
            if m:
                return m.group(1).strip()[:200]
        return None

    def _extract_partes(self, html):
        """Extrai nomes de partes (autor, reu) da listagem."""
        partes = []
        for padrao in [
            r"Parte Autora:?\s*([^<\n]+)",
            r"Parte R[eé]:?\s*([^<\n]+)",
            r"Autor:?\s*([^<\n]+)",
            r"R[eé]u:?\s*([^<\n]+)",
        ]:
            m = re.findall(padrao, html, re.IGNORECASE)
            for match in m:
                t = re.sub(r"\s+", " ", match).strip()[:200]
                if t and t not in partes:
                    partes.append(t)
        return partes[:10]

    def close(self):
        with self._lock:
            if self.driver:
                try:
                    self.driver.quit()
                except Exception:
                    pass
                self.driver = None
            self._logged_in = False
