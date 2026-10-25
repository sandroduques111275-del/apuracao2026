#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Apuração em tempo real — 2º turno 25/10/2026 (Presidente + Governadores)
=========================================================================
Lê os arquivos JSON oficiais de divulgação do TSE (resultados.tse.jus.br),
calcula uma PROJEÇÃO do resultado final por estado e mostra num painel
local em http://localhost:8080

Só usa a biblioteca padrão do Python 3.8+ (nada para instalar).

Uso:
    python3 servidor.py                 # dia da eleição (2º turno, automático)
    python3 servidor.py --teste-1turno  # testa hoje com os arquivos do 1º turno
    python3 servidor.py --demo          # simulação de uma noite de apuração
    python3 servidor.py --host 0.0.0.0  # para abrir também no celular (mesma rede)

AVISO: a projeção é uma estimativa estatística, não é resultado oficial.
"""
import argparse
import bisect
import json
import math
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# Configuração (códigos oficiais publicados pelo TSE para 2026)
# --------------------------------------------------------------------------
BASE = "https://resultados.tse.jus.br/oficial"
CICLO = "ele2026"
URL_CONFIG = BASE + "/comum/config/ele-c.json"

PRESIDENTE = {"id": "pres", "titulo": "Presidente da República", "cargo": 1,
              "ele1t": "6257", "ele2t": "6258"}
GOVERNADOR = {"cargo": 3, "ele1t": "6259", "ele2t": "6260",
              "ufs": ["ac", "am", "df", "es", "rj", "rn", "to"]}  # UFs com 2º turno

# Ordem dos candidatos no painel (número de urna). Cores neutras.
CANDIDATOS_PRES = ["22", "13"]  # 1º = Flávio (azul), 2º = Lula (vermelho)
INTERVALO_SEG = 15          # intervalo entre leituras do TSE (segundos)
INTERVALO_FINAL_SEG = 300   # depois de 100% apurado
ESPERA_404_SEG = 120        # quando o TSE ainda não publicou o arquivo

UFS = ["ac", "al", "ap", "am", "ba", "ce", "df", "es", "go", "ma", "mt", "ms", "mg", "pa",
       "pb", "pr", "pe", "pi", "rj", "rn", "rs", "ro", "rr", "sc", "sp", "se", "to", "zz"]
NOMES = {"ac": "Acre", "al": "Alagoas", "ap": "Amapá", "am": "Amazonas", "ba": "Bahia",
         "ce": "Ceará", "df": "Distrito Federal", "es": "Espírito Santo", "go": "Goiás",
         "ma": "Maranhão", "mt": "Mato Grosso", "ms": "Mato Grosso do Sul", "mg": "Minas Gerais",
         "pa": "Pará", "pb": "Paraíba", "pr": "Paraná", "pe": "Pernambuco", "pi": "Piauí",
         "rj": "Rio de Janeiro", "rn": "Rio Grande do Norte", "rs": "Rio Grande do Sul",
         "ro": "Rondônia", "rr": "Roraima", "sc": "Santa Catarina", "sp": "São Paulo",
         "se": "Sergipe", "to": "Tocantins", "zz": "Exterior", "br": "Brasil"}
REGIAO = {"ac": "Norte", "ap": "Norte", "am": "Norte", "pa": "Norte", "ro": "Norte",
          "rr": "Norte", "to": "Norte", "al": "Nordeste", "ba": "Nordeste", "ce": "Nordeste",
          "ma": "Nordeste", "pb": "Nordeste", "pe": "Nordeste", "pi": "Nordeste",
          "rn": "Nordeste", "se": "Nordeste", "df": "Centro-Oeste", "go": "Centro-Oeste",
          "mt": "Centro-Oeste", "ms": "Centro-Oeste", "es": "Sudeste", "mg": "Sudeste",
          "rj": "Sudeste", "sp": "Sudeste", "pr": "Sul", "rs": "Sul", "sc": "Sul",
          "zz": "Exterior"}
ORDEM_REGIOES = ["Norte", "Nordeste", "Centro-Oeste", "Sudeste", "Sul", "Exterior"]

AQUI = os.path.dirname(os.path.abspath(__file__))
PASTA_CACHE = os.path.join(AQUI, "cache_1turno")


def num(x):
    """Converte '1.234', '95,12', 123 ou None em float."""
    if x is None or x == "":
        return 0.0
    try:
        return float(str(x).replace(",", "."))
    except ValueError:
        return 0.0


def url_resultado(ele, uf, cargo):
    return f"{BASE}/{CICLO}/{ele}/dados/{uf}/{uf}-c{int(cargo):04d}-e{int(ele):06d}-u.json"


# --------------------------------------------------------------------------
# Acesso ao TSE (com ETag e cuidado com 404 — 404 em excesso bloqueia o IP)
# --------------------------------------------------------------------------
class ClienteTSE:
    def __init__(self):
        self.etags = {}
        self.cache = {}
        self.ultimo_404 = {}
        self.requisicoes = 0

    def get(self, url):
        """Retorna o JSON (dict) ou None se o arquivo ainda não existe (404)."""
        quando = self.ultimo_404.get(url)
        if quando and time.time() - quando < ESPERA_404_SEG:
            return None
        req = urllib.request.Request(url, headers={
            "User-Agent": "painel-apuracao-pessoal/1.0",
            "Accept": "application/json"})
        if url in self.etags:
            req.add_header("If-None-Match", self.etags[url])
        self.requisicoes += 1
        time.sleep(0.12)  # bem abaixo do limite de 100 req/s do TSE
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                dados = json.loads(r.read().decode("utf-8"))
                et = r.headers.get("ETag")
                if et:
                    self.etags[url] = et
                self.cache[url] = dados
                self.ultimo_404.pop(url, None)
                return dados
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return self.cache.get(url)
            if e.code in (403, 404):
                self.ultimo_404[url] = time.time()
                return None
            raise


def ler_unidade(d, cargo):
    """Converte o arquivo '-u.json' do TSE num dicionário simples."""
    cargos = d.get("carg") or []
    carg = next((c for c in cargos if str(c.get("cd")) == str(cargo)), cargos[0] if cargos else {})
    cands = {}
    for agr in carg.get("agr", []) or []:
        for par in agr.get("par", []) or []:
            for c in par.get("cand", []) or []:
                cands[str(c.get("n"))] = {"nome": c.get("nmu") or c.get("nm") or str(c.get("n")),
                                          "votos": num(c.get("vap")), "st": c.get("st")}
    s, e, v = d.get("s") or {}, d.get("e") or {}, d.get("v") or {}
    ts, st = num(s.get("ts")), num(s.get("st"))
    pst = num(s.get("pst")) if s.get("pst") not in (None, "") else (100 * st / ts if ts else 0)
    return {"ts": ts, "st": st, "pst": pst, "te": num(e.get("te")), "est": num(e.get("est")),
            "c": num(e.get("c")), "a": num(e.get("a")), "vv": num(v.get("vv")),
            "vb": num(v.get("vb")), "tvn": num(v.get("tvn")), "cands": cands,
            "dt": d.get("dt"), "ht": d.get("ht"), "and": d.get("and")}


# --------------------------------------------------------------------------
# Modelo de projeção
# --------------------------------------------------------------------------
def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def projetar(unidades, prior, par):
    """
    unidades: {uf: unidade do 2º turno (apuração atual)}
    prior:    {uf: unidade do 1º turno} (base histórica)
    par:      [numA, numB] — os dois candidatos

    Para cada UF:
      1) votos que faltam = eleitores das seções não apuradas × (votos válidos ÷ eleitores
         já apurados na própria UF). Sem dado ainda, usa a taxa do 1º turno.
      2) % do candidato A nos votos que faltam = mistura entre
           - o % já apurado na UF (peso cresce com √(% apurado)), e
           - a "base": % de A no 1º turno (só A×B) + a variação média (swing) que as
             UFs já apuradas mostram em relação ao 1º turno.
      3) soma tudo → resultado projetado nacional + margem de erro → chance de vitória.
    """
    A, B = par
    nomes = {}
    for u in list(unidades.values()) + list(prior.values()):
        for n in par:
            if n in u["cands"] and n not in nomes:
                nomes[n] = u["cands"][n]["nome"]

    def base_a(uf):
        p = prior.get(uf)
        if not p:
            return 0.5
        a, b = p["cands"].get(A, {}).get("votos", 0), p["cands"].get(B, {}).get("votos", 0)
        return a / (a + b) if a + b > 0 else 0.5

    # swing nacional (só faz sentido com várias UFs)
    soma_w, soma_dif, apurado_vv, esperado_vv = 0.0, 0.0, 0.0, 0.0
    for uf, u in unidades.items():
        a, b = u["cands"].get(A, {}).get("votos", 0), u["cands"].get(B, {}).get("votos", 0)
        if a + b > 0 and uf in prior:
            soma_w += a + b
            soma_dif += (a + b) * (a / (a + b) - base_a(uf))
        apurado_vv += a + b
        p = prior.get(uf)
        esperado_vv += (p["vv"] if p and p["vv"] else u["te"] * 0.75)
    swing = soma_dif / soma_w if soma_w else 0.0
    if len(unidades) <= 1:
        swing = 0.0
    else:
        swing *= min(1.0, apurado_vv / (0.03 * esperado_vv)) if esperado_vv else 0
    cobertura_nac = min(1.0, apurado_vv / esperado_vv) if esperado_vv else 0

    linhas = []
    tot = {"a": 0.0, "b": 0.0, "pa": 0.0, "pb": 0.0, "falta": 0.0, "ts": 0, "st": 0}
    var_ind, desvio_corr = 0.0, 0.0
    for uf, u in unidades.items():
        a, b = u["cands"].get(A, {}).get("votos", 0), u["cands"].get(B, {}).get("votos", 0)
        frac = u["st"] / u["ts"] if u["ts"] else 0.0
        resta_el = max(0.0, u["te"] - u["est"])
        p = prior.get(uf)
        if u["est"] > 0 and a + b > 0:
            taxa = (a + b) / u["est"]
        elif p and p["te"]:
            taxa = p["vv"] / p["te"]
        else:
            taxa = 0.74
        falta = 0.0 if frac >= 0.99999 else resta_el * taxa
        share_ap = a / (a + b) if a + b > 0 else None
        base = min(0.99, max(0.01, base_a(uf) + swing))
        w = math.sqrt(frac) if share_ap is not None else 0.0
        s_resto = w * share_ap + (1 - w) * base if share_ap is not None else base
        s_resto = min(0.99, max(0.01, s_resto))
        pa, pb = a + falta * s_resto, b + falta * (1 - s_resto)
        # incerteza do % dos votos que faltam
        sd = math.sqrt((w * 0.035 * (1 - frac)) ** 2 + ((1 - w) * 0.045) ** 2) + 0.005
        var_ind += (2 * falta * sd) ** 2
        desvio_corr += 2 * falta * 0.02 * (1 - cobertura_nac)
        linhas.append({
            "uf": uf, "nome": NOMES.get(uf, uf.upper()), "regiao": REGIAO.get(uf, ""),
            "pst": round(u["pst"], 2), "a": a, "b": b,
            "pct_a": round(100 * share_ap, 2) if share_ap is not None else None,
            "proj_a": round(100 * pa / (pa + pb), 2) if pa + pb else None,
            "base_a": round(100 * base_a(uf), 2) if uf in prior else None,
            "falta": round(falta), "eleitorado": u["te"],
            "hora": u.get("ht"), "and": u.get("and")})
        tot["a"] += a; tot["b"] += b; tot["pa"] += pa; tot["pb"] += pb
        tot["falta"] += falta; tot["ts"] += u["ts"]; tot["st"] += u["st"]

    margem = tot["pa"] - tot["pb"]
    sd_total = math.sqrt(var_ind) + desvio_corr
    if tot["falta"] < 1:
        prob_a = 1.0 if margem > 0 else (0.0 if margem < 0 else 0.5)
    elif sd_total > 0:
        prob_a = phi(margem / sd_total)
    else:
        prob_a = 0.5
    apurada = tot["a"] - tot["b"]
    definido = None
    if tot["a"] + tot["b"] > 0 and abs(apurada) > tot["falta"] * 1.10:
        definido = A if apurada > 0 else B  # nem com 100% dos votos restantes vira

    lider = A if prob_a >= 0.5 else B
    pr = max(prob_a, 1 - prob_a)
    if definido:
        confianca = "matematicamente definido"
    elif pr >= 0.99:
        confianca = "muito alta"
    elif pr >= 0.90:
        confianca = "alta"
    elif pr >= 0.70:
        confianca = "moderada"
    else:
        confianca = "disputa aberta"

    # agregação por região
    regs = {}
    for l in linhas:
        r = regs.setdefault(l["regiao"], {"regiao": l["regiao"], "a": 0, "b": 0,
                                         "pa": 0.0, "pb": 0.0, "eleitorado": 0, "ts": 0, "st": 0})
        u = unidades[l["uf"]]
        r["a"] += l["a"]; r["b"] += l["b"]; r["eleitorado"] += l["eleitorado"]
        r["ts"] += u["ts"]; r["st"] += u["st"]
        tot_proj = l["a"] + l["b"] + l["falta"]
        r["pa"] += tot_proj * (l["proj_a"] or 0) / 100
        r["pb"] += tot_proj * (100 - (l["proj_a"] or 0)) / 100
    regioes = []
    for nome in ORDEM_REGIOES:
        if nome in regs:
            r = regs[nome]
            regioes.append({"regiao": nome,
                            "pst": round(100 * r["st"] / r["ts"], 2) if r["ts"] else 0,
                            "pct_a": round(100 * r["a"] / (r["a"] + r["b"]), 2) if r["a"] + r["b"] else None,
                            "proj_a": round(100 * r["pa"] / (r["pa"] + r["pb"]), 2) if r["pa"] + r["pb"] else None,
                            "peso": round(100 * (r["pa"] + r["pb"]) / (tot["pa"] + tot["pb"]), 1)
                            if tot["pa"] + tot["pb"] else 0})

    return {
        "candidatos": [{"n": A, "nome": nomes.get(A, A)}, {"n": B, "nome": nomes.get(B, B)}],
        "apurado": {"a": tot["a"], "b": tot["b"],
                    "pct_a": round(100 * tot["a"] / (tot["a"] + tot["b"]), 2) if tot["a"] + tot["b"] else None},
        "projecao": {"a": round(tot["pa"]), "b": round(tot["pb"]),
                     "pct_a": round(100 * tot["pa"] / (tot["pa"] + tot["pb"]), 2) if tot["pa"] + tot["pb"] else None,
                     "margem_erro_pp": round(100 * 1.96 * sd_total / (tot["pa"] + tot["pb"]) / 2, 2)
                     if tot["pa"] + tot["pb"] else None},
        "falta_votos": round(tot["falta"]),
        "pst_soma": round(100 * tot["st"] / tot["ts"], 2) if tot["ts"] else 0,
        "prob_a": round(prob_a, 4), "lider": lider, "confianca": confianca,
        "definido": definido, "swing_pp": round(100 * swing, 2),
        "ufs": sorted(linhas, key=lambda x: x["uf"]), "regioes": regioes,
    }


# --------------------------------------------------------------------------
# Modo demonstração (dados fictícios no MESMO formato do TSE)
# --------------------------------------------------------------------------
ELEITORADO_APROX = {"sp": 34.4, "mg": 16.2, "rj": 12.8, "ba": 11.3, "rs": 8.6, "pr": 8.5,
                    "pe": 7.0, "ce": 6.8, "pa": 6.1, "sc": 5.5, "ma": 5.0, "go": 4.9,
                    "pb": 3.1, "es": 2.9, "am": 2.6, "rn": 2.6, "pi": 2.6, "mt": 2.5,
                    "al": 2.4, "df": 2.2, "ms": 2.0, "se": 1.7, "ro": 1.2, "to": 1.1,
                    "zz": 0.9, "ac": 0.6, "ap": 0.55, "rr": 0.37}
BASE_REGIONAL_DEMO = {"Nordeste": 0.66, "Norte": 0.48, "Sudeste": 0.47, "Sul": 0.39,
                      "Centro-Oeste": 0.40, "Exterior": 0.50}


def montar_json_tse(uf, cargo, ts, st, te, est, c, votos, nomes, dt="25/10/2026", ht="19:00:00"):
    vv = sum(votos.values())
    return {"cdabr": uf, "dt": dt, "ht": ht, "and": "f" if st >= ts else "p",
            "s": {"ts": str(int(ts)), "st": str(int(st)), "pst": f"{100 * st / ts:.2f}".replace(".", ",")},
            "e": {"te": str(int(te)), "est": str(int(est)), "c": str(int(c)), "a": str(int(est - c))},
            "v": {"vv": str(int(vv)), "vb": str(int(c * 0.015)), "tvn": str(int(c * 0.03))},
            "carg": [{"cd": str(cargo), "agr": [{"par": [{"cand": [
                {"n": n, "nmu": nomes[n], "vap": str(int(v))} for n, v in votos.items()]}]}]}]}


PLEITO_1T = "3220"                      # pleito do 1º turno de 04/10/2026
INICIO_APURACAO = (2026, 10, 25, 17, 0, 0)  # relógio da simulação começa às 17h de 25/10


def url_cs(uf, pleito=PLEITO_1T):
    """EA16 — configuração de seções da UF (traz data/hora de chegada de cada seção)."""
    return f"{BASE}/{CICLO}/arquivo-urna/{pleito}/config/{uf}/{uf}-p{int(pleito):06d}-cs.json"


def url_mun(ele, uf, mun, cargo=1):
    return f"{BASE}/{CICLO}/{ele}/dados/{uf}/{uf}{mun}-c{int(cargo):04d}-e{int(ele):06d}-u.json"


def seg_desde_inicio(da, ha):
    import datetime
    try:
        d, m, y = str(da).split("/")
        H, M, S = str(ha).split(":")
        t = datetime.datetime(int(y), int(m), int(d), int(H), int(M), int(S))
        return int((t - datetime.datetime(*INICIO_APURACAO)).total_seconds())
    except Exception:
        return None


def secoes_do_cs(cs):
    """Percorre o -cs.json e devolve {município: [segundos de chegada...]} e {município: nº de seções}."""
    tempos, total = {}, {}

    def walk(n, mun, chave_pai):
        if isinstance(n, dict):
            cd = n.get("cd")
            if isinstance(cd, str) and len(cd) == 5 and cd.isdigit():
                mun = cd
            if ("ns" in n or chave_pai == "sec") and mun:
                ns, nsp = n.get("ns"), n.get("nsp")
                if nsp in (None, "", ns):  # só a seção principal (agregadas chegam junto)
                    total[mun] = total.get(mun, 0) + 1
                    sg = seg_desde_inicio(n.get("da"), n.get("ha")) if n.get("ha") else None
                    if sg is not None:
                        tempos.setdefault(mun, []).append(max(0, sg))
                return
            for k, v in n.items():
                if isinstance(v, (list, dict)):
                    walk(v, mun, k)
        elif isinstance(n, list):
            for x in n:
                walk(x, mun, chave_pai)

    walk(cs, None, None)
    return tempos, total


ESPERA_FIM_SIM = 15  # segundos mostrando o resultado final antes de zerar a tela
VEL_SIM = 480.0   # velocidade fixa da simulação: 8 min de apuração por segundo (noite toda em ~45 s)


class Demo:
    """Simulação da noite de apuração.
    - Com a "linha do tempo" do 1º turno: cada município entra na contagem exatamente no ritmo
      em que as SUAS seções chegaram ao TSE em 04/10 (horário de cada seção, arquivo EA16),
      com os votos reais daquele município. É o replay da apuração real.
    - Sem ela: modelo aproximado (Sul/Sudeste antes, Norte/Nordeste depois)."""

    def __init__(self, minutos, semente=None, prior_real=None, aleatoria=False, linha=None):
        rnd = random.Random(semente)
        self.aleatoria = aleatoria
        self.real = bool(prior_real)
        self.nomes = {"13": "CANDIDATO 13 (fictício)", "22": "CANDIDATO 22 (fictício)"}
        if self.real:  # nomes reais vindos do arquivo do TSE
            for n in ("13", "22"):
                for u in prior_real.values():
                    if n in u["cands"]:
                        self.nomes[n] = u["cands"][n]["nome"]
                        break
        swing = rnd.uniform(-0.03, 0.03) if aleatoria or not self.real else 0.0
        ruido = 0.015 if aleatoria or not self.real else 0.0
        self.uf = {}
        self.prior = {}
        for uf in UFS:
            reg = REGIAO[uf]
            velocidade = {"Sudeste": 1.25, "Sul": 1.3, "Centro-Oeste": 1.1, "Nordeste": 0.85,
                          "Norte": 0.75, "Exterior": 0.6}[reg] * rnd.uniform(0.85, 1.15)
            p1 = prior_real.get(uf) if self.real else None
            if p1 and p1["te"] > 0 and p1["c"] > 0:
                te, ts = p1["te"], max(1, int(p1["ts"]))
                comp, valid = p1["c"] / p1["te"], p1["vv"] / p1["c"]
                a1 = p1["cands"].get("13", {}).get("votos", 0)
                b1 = p1["cands"].get("22", {}).get("votos", 0)
                base = a1 / (a1 + b1) if a1 + b1 else 0.5
                self.prior[uf] = p1
            else:
                te, ts = ELEITORADO_APROX[uf] * 1e6, 1000
                comp, valid = rnd.uniform(0.74, 0.84), 0.95
                base = BASE_REGIONAL_DEMO[reg] + rnd.uniform(-0.06, 0.06)
                vv1 = te * 0.76
                a1 = vv1 * base * 0.93
                self.prior[uf] = ler_unidade(montar_json_tse(
                    uf, 1, 1000, 1000, te, te, te * 0.8, {"13": a1, "22": vv1 * 0.93 - a1, "70": vv1 * 0.07},
                    {**self.nomes, "70": "OUTRO"}, dt="04/10/2026"), 1)
            self.uf[uf] = {"te": te, "ts": ts, "verdade": base + swing + rnd.uniform(-ruido, ruido),
                           "vel": velocidade, "vies": rnd.uniform(-0.06, 0.06),
                           "comp": comp * (rnd.uniform(0.97, 1.01) if ruido else 1.0), "valid": valid}

        # linha do tempo real (lista de municípios: [uf, te, c, votos13, votos22, [segundos...]])
        self.linha = linha if (linha and not aleatoria) else None
        self.ufs_linha = {m[0] for m in self.linha} if self.linha else set()
        if self.linha:
            self.fim = max(1, max((m[5][-1] for m in self.linha if m[5]), default=1))
        else:
            self.fim = 6 * 3600                       # 17h às 23h de apuração simulada
        self.vel = VEL_SIM
        self.v = 0.0
        self._ult = time.time()
        self.pausado = True                           # espera alguém tocar em "Iniciar simulação"
        self.terminou_em = None

    # ---- relógio da simulação ----
    def avancar(self):
        agora = time.time()
        if not self.pausado:
            self.v = min(self.fim, self.v + (agora - self._ult) * self.vel)
        self._ult = agora
        if self.v >= self.fim and self.terminou_em is None:
            self.terminou_em = agora

    def reiniciar(self):
        self.v, self._ult, self.terminou_em, self.pausado = 0.0, time.time(), None, False
        if not self.linha and self.real:
            # nova rodada = novo resultado sorteado (mantém a velocidade escolhida)
            vel = self.vel
            self.__init__(self.fim / 360, prior_real=self.prior, aleatoria=self.aleatoria)
            self.vel = vel

    def hora(self):
        import datetime
        t = datetime.datetime(*INICIO_APURACAO) + datetime.timedelta(seconds=int(self.v))
        return t.strftime("%d/%m %H:%M")

    def unidades(self):
        self.avancar()
        t = min(1.0, self.v / self.fim)
        out = {}
        hora = self.hora()
        if self.linha:
            acc = {}
            for uf, te, c, a, b, tempos in self.linha:
                n = len(tempos)
                k = bisect.bisect_right(tempos, self.v)
                f = k / n if n else 0.0
                x = acc.setdefault(uf, [0, 0, 0.0, 0.0, 0.0, 0.0, 0.0])
                x[0] += n; x[1] += k; x[2] += te; x[3] += te * f
                x[4] += c * f; x[5] += a * f; x[6] += b * f
            for uf, (ts, st, te, est, c, a, b) in acc.items():
                out[uf] = ler_unidade(montar_json_tse(
                    uf, 1, max(ts, 1), st, te, est, c, {"13": a, "22": b}, self.nomes,
                    dt="25/10/2026", ht=hora), 1)
        for uf, p in self.uf.items():
            if uf in out:
                continue
            frac = min(1.0, max(0.0, t ** (1.0 / p["vel"])))
            ts = p["ts"]
            st = round(ts * frac)
            frac = st / ts
            est = p["te"] * frac
            c = est * p["comp"]
            vv = c * p["valid"]
            share = p["verdade"] + p["vies"] * (1 - frac) ** 1.5
            out[uf] = ler_unidade(montar_json_tse(
                uf, 1, ts, st, p["te"], est, c, {"13": vv * share, "22": vv * (1 - share)},
                self.nomes, ht=hora), 1)
        return out, t

    def resultado_real(self):
        """Resultado final da simulação (% do 13 entre os dois), para conferir a projeção."""
        a = b = 0.0
        if self.linha:
            for m in self.linha:
                a += m[3]; b += m[4]
        for uf, p in self.uf.items():
            if uf in self.ufs_linha:
                continue
            vv = p["te"] * p["comp"] * p["valid"]
            a += vv * p["verdade"]; b += vv * (1 - p["verdade"])
        return round(100 * a / (a + b), 2) if a + b else None

    def info(self):
        estado = "fim" if self.v >= self.fim else ("parado" if self.v == 0 and self.pausado else "rodando")
        return {"estado": estado, "vel": self.vel, "pausado": self.pausado, "hora": self.hora(),
                "pct_tempo": round(100 * self.v / self.fim, 1),
                "ordem": "real" if self.linha else "modelo",
                "ufs_reais": len(self.ufs_linha)}


# --------------------------------------------------------------------------
# Coletor (thread que lê o TSE de tempos em tempos)
# --------------------------------------------------------------------------
class Coletor(threading.Thread):
    def __init__(self, args):
        super().__init__(daemon=True)
        self.args = args
        self.cli = ClienteTSE()
        self.lock = threading.Lock()
        self.estado = {"status": "iniciando", "mensagem": "Iniciando…", "historico": []}
        self.prior_pres = {}
        self.prior_gov = {}
        self.demo = None  # criado em run() (precisa da base do 1º turno)
        self.acordar = threading.Event()
        self.ele_pres = PRESIDENTE["ele1t"] if args.teste_1turno else PRESIDENTE["ele2t"]
        self.ele_gov = GOVERNADOR["ele1t"] if args.teste_1turno else GOVERNADOR["ele2t"]
        if args.eleicao:
            self.ele_pres = args.eleicao

    # ---- base do 1º turno (baixada uma vez e guardada em disco) ----
    def carregar_prior(self):
        os.makedirs(PASTA_CACHE, exist_ok=True)

        def um(ele, uf, cargo):
            arq = os.path.join(PASTA_CACHE, f"{ele}-{uf}-c{cargo}.json")
            if os.path.exists(arq):
                with open(arq, encoding="utf-8") as f:
                    return ler_unidade(json.load(f), cargo)
            d = self.cli.get(url_resultado(ele, uf, cargo))
            if d is None:
                return None
            if (d.get("and") == "f") or num((d.get("s") or {}).get("pst")) >= 100:
                with open(arq, "w", encoding="utf-8") as f:
                    json.dump(d, f)
            return ler_unidade(d, cargo)

        for uf in UFS:
            try:
                u = um(PRESIDENTE["ele1t"], uf, 1)
                if u:
                    self.prior_pres[uf] = u
            except Exception as e:  # rede instável: segue sem a base daquela UF
                print(f"  aviso: base 1º turno {uf}: {e}")
        for uf in GOVERNADOR["ufs"]:
            try:
                u = um(GOVERNADOR["ele1t"], uf, 3)
                if u:
                    self.prior_gov[uf] = u
            except Exception as e:
                print(f"  aviso: base 1º turno governador {uf}: {e}")
        print(f"Base do 1º turno: {len(self.prior_pres)} UFs (presidente), "
              f"{len(self.prior_gov)} UFs (governador)")

    def _msg(self, texto):
        print("  " + texto)
        with self.lock:
            if self.estado.get("status") != "ok":
                self.estado = {"status": "carregando", "mensagem": texto, "historico": []}

    def carregar_linha_tempo(self):
        """Ordem REAL de chegada das urnas no 1º turno: só o horário de cada seção (28 arquivos,
        um por UF). Os votos usados são os totais oficiais de cada UF, entrando na proporção
        das seções que já tinham chegado naquele horário."""
        arq = os.path.join(PASTA_CACHE, "horarios_1turno.json")
        horarios = None
        if os.path.exists(arq):
            try:
                with open(arq, encoding="utf-8") as f:
                    horarios = json.load(f)
            except Exception:
                horarios = None
        if horarios is None:
            horarios = {}
            for uf in UFS:
                self._msg(f"Baixando o horário de chegada das urnas do 1º turno: {uf.upper()}…")
                try:
                    cs = self.cli.get(url_cs(uf))
                except Exception as e:
                    print(f"  aviso: seções {uf}: {e}")
                    cs = None
                if not cs:
                    print(f"  aviso: sem horário das seções de {uf.upper()} — essa UF usa o modelo aproximado")
                    continue
                tempos, total = secoes_do_cs(cs)
                tt = sorted(t for v in tempos.values() for t in v)
                if not tt:
                    print(f"  aviso: {uf.upper()} sem data/hora nas seções — essa UF usa o modelo aproximado")
                    continue
                faltam = sum(total.values()) - len(tt)
                tt += [tt[-1]] * max(0, faltam)  # seção sem horário entra junto com a última
                horarios[uf] = tt
            if horarios:
                os.makedirs(PASTA_CACHE, exist_ok=True)
                with open(arq, "w", encoding="utf-8") as f:
                    json.dump(horarios, f)
        linha = []
        for uf, tt in horarios.items():
            p1 = self.prior_pres.get(uf)
            if not p1 or not tt:
                continue
            linha.append([uf, p1["te"], p1["c"], p1["cands"].get("13", {}).get("votos", 0),
                          p1["cands"].get("22", {}).get("votos", 0), tt])
        if linha:
            print(f"  ordem real do 1º turno pronta: {len(linha)} UFs, {sum(len(m[5]) for m in linha)} seções")
        return linha or None

    def descobrir_codigos(self):
        """Confere no ele-c.json do TSE se o código do 2º turno mudou."""
        try:
            cfg = self.cli.get(URL_CONFIG)
            if not cfg:
                return
            texto = json.dumps(cfg)
            for chave, alvo in ((PRESIDENTE["ele1t"], "pres"), (GOVERNADOR["ele1t"], "gov")):
                for p in cfg.get("pl", []) or []:
                    for e in p.get("e", []) or []:
                        if str(e.get("cd")) == chave and e.get("cdt2"):
                            novo = str(e["cdt2"])
                            if alvo == "pres" and not self.args.teste_1turno and not self.args.eleicao:
                                self.ele_pres = novo
                            if alvo == "gov" and not self.args.teste_1turno:
                                self.ele_gov = novo
            if "6258" not in texto and not self.args.teste_1turno:
                print("  aviso: ele-c.json não cita 6258; usando os códigos configurados.")
        except Exception as e:
            print(f"  aviso: não consegui ler o ele-c.json ({e}); usando códigos configurados.")

    def ciclo_real(self):
        cli = self.cli
        # se a base do 1º turno não veio completa (rede instável), tenta de novo a cada 5 min
        if len(self.prior_pres) < len(UFS) and time.time() - getattr(self, "_ult_prior", 0) > 300:
            self._ult_prior = time.time()
            self.carregar_prior()
        br = cli.get(url_resultado(self.ele_pres, "br", 1))
        if br is None:
            return {"status": "aguardando",
                    "mensagem": f"O TSE ainda não publicou os arquivos da eleição {self.ele_pres}. "
                                f"Nova tentativa em {ESPERA_404_SEG // 60} min (a votação termina às 17h de Brasília)."}
        unidades = {}
        for uf in UFS:
            d = cli.get(url_resultado(self.ele_pres, uf, 1))
            if d is not None:
                unidades[uf] = ler_unidade(d, 1)
        u_br = ler_unidade(br, 1)
        par = self.escolher_par(u_br, CANDIDATOS_PRES)
        pres = projetar(unidades, self.prior_pres, par)
        pres["oficial_br"] = {"pst": u_br["pst"], "hora": u_br["ht"], "data": u_br["dt"],
                              "votos": {n: c["votos"] for n, c in u_br["cands"].items()},
                              "vv": u_br["vv"], "and": u_br["and"]}
        # O total NACIONAL exibido vem do arquivo do Brasil (o mesmo do app do TSE).
        # Os arquivos dos estados às vezes chegam atrasados na CDN do TSE; eles seguem
        # sendo usados só para a projeção e as tabelas por estado/região.
        A, B = par
        a_br = u_br["cands"].get(A, {}).get("votos", 0)
        b_br = u_br["cands"].get(B, {}).get("votos", 0)
        if a_br + b_br >= pres["apurado"]["a"] + pres["apurado"]["b"]:
            pres["apurado"] = {"a": a_br, "b": b_br,
                               "pct_a": round(100 * a_br / (a_br + b_br), 2) if a_br + b_br else None}
            pres["pst_soma"] = round(u_br["pst"], 2)
            if pres["definido"] and abs(a_br - b_br) <= pres["falta_votos"] * 1.10:
                pres["definido"] = None
            if not pres["definido"] and a_br + b_br > 0 and abs(a_br - b_br) > pres["falta_votos"] * 1.10 \
                    and u_br["pst"] >= 50:
                pres["definido"] = A if a_br > b_br else B
                pres["confianca"] = "matematicamente definido"
        govs = []
        primeira = True
        for uf in GOVERNADOR["ufs"]:
            d = cli.get(url_resultado(self.ele_gov, uf, 3))
            if d is None:
                if primeira:
                    break  # ainda não publicado: não insiste nas outras (evita 404 em série)
                continue
            primeira = False
            u = ler_unidade(d, 3)
            par_g = self.escolher_par(u, None, self.prior_gov.get(uf))
            if len(par_g) == 2:
                g = projetar({uf: u}, {uf: self.prior_gov[uf]} if uf in self.prior_gov else {}, par_g)
                g["uf"], g["nome_uf"] = uf, NOMES[uf]
                govs.append(g)
        return {"status": "ok", "presidente": pres, "governadores": govs}

    @staticmethod
    def escolher_par(u, preferidos=None, prior=None):
        """Os dois candidatos da disputa (no 2º turno só existem dois)."""
        if preferidos and all(n in u["cands"] for n in preferidos):
            return list(preferidos)
        ordem = sorted(u["cands"].items(), key=lambda kv: -kv[1]["votos"])
        if len(ordem) >= 2 and ordem[0][1]["votos"] > 0:
            return [ordem[0][0], ordem[1][0]]
        if prior:
            ordem = sorted(prior["cands"].items(), key=lambda kv: -kv[1]["votos"])
        return [k for k, _ in ordem[:2]]

    def ciclo_demo(self):
        d = self.demo
        if d.terminou_em and time.time() - d.terminou_em > ESPERA_FIM_SIM:
            # acabou: mostra o resultado final por alguns segundos e volta à tela inicial (17:00, zerada)
            d.reiniciar()
            d.pausado = True
            with self.lock:
                self.estado["historico"] = []
        unidades, t = d.unidades()
        pres = projetar(unidades, d.prior, CANDIDATOS_PRES)
        pres["oficial_br"] = {"pst": pres["pst_soma"], "hora": d.hora(),
                              "data": "25/10/2026", "and": "f" if t >= 1 else "p"}
        r13 = d.resultado_real()
        if r13 is not None:
            pres["real_demo_a"] = r13 if CANDIDATOS_PRES[0] == "13" else round(100 - r13, 2)
        return {"status": "ok", "presidente": pres, "governadores": [], "demo": d.info()}

    def controle(self, vel=None, acao=None):
        d = self.demo
        if not d:
            return {"ok": False, "erro": "não está em modo simulação"}
        d.avancar()
        if vel is not None:
            d.vel = max(0.0, min(20000.0, float(vel)))
            d.pausado = d.vel == 0
        if acao == "pausar":
            d.pausado = True
        elif acao == "continuar":
            d.pausado = False
            if d.vel == 0:
                d.vel = 900.0
        elif acao == "iniciar":
            d.reiniciar()
            d.vel, d.pausado = VEL_SIM, False
            with self.lock:
                self.estado["historico"] = []
        elif acao == "reiniciar":
            d.reiniciar()
            with self.lock:
                self.estado["historico"] = []
        self.acordar.set()
        return {"ok": True, "demo": d.info()}

    def run(self):
        if self.args.demo:
            print("Simulação: carregando a base do 1º turno (TSE)…")
            self.carregar_prior()
            real = self.prior_pres if len(self.prior_pres) >= 27 else None
            if not real:
                print("  sem acesso ao 1º turno — usando números fictícios.")
            # simulação: votos de cada estado = 1º turno (Flávio × Lula); só a ORDEM da apuração é simulada.
            # --demo-aleatoria sorteia uma variação nacional de até ±3 p.p.
            self.demo = Demo(self.args.demo_minutos, prior_real=real, aleatoria=self.args.demo_aleatoria)
            print("  simulação: resultado de cada estado igual ao 1º turno" if real and not self.args.demo_aleatoria
                  else "  simulação com resultado sorteado")
        else:
            print("Lendo configuração e base do 1º turno no TSE…")
            self.descobrir_codigos()
            self._ult_prior = time.time()
            self.carregar_prior()
        while True:
            espera = INTERVALO_SEG if not self.demo else 1
            try:
                r = self.ciclo_demo() if self.demo else self.ciclo_real()
                r["modo"] = ("demo" if self.demo else "teste-1turno" if self.args.teste_1turno else "oficial")
                r["eleicao"] = "simulação" if self.demo else self.ele_pres
                r["atualizado"] = time.strftime("%H:%M:%S", time.gmtime(time.time() - 3 * 3600))  # Brasília
                r["requisicoes"] = self.cli.requisicoes
                with self.lock:
                    hist = self.estado.get("historico", [])
                    if r.get("status") == "ok":
                        p = r["presidente"]
                        ponto = {"pst": p["pst_soma"], "apurado_a": p["apurado"]["pct_a"],
                                 "proj_a": p["projecao"]["pct_a"], "prob_a": p["prob_a"],
                                 "hora": r["atualizado"]}
                        if not hist or hist[-1]["pst"] != ponto["pst"] or hist[-1]["apurado_a"] != ponto["apurado_a"]:
                            hist.append(ponto)
                        if p["pst_soma"] >= 100 and not self.demo:
                            espera = INTERVALO_FINAL_SEG
                    else:
                        espera = ESPERA_404_SEG
                    r["historico"] = hist[-400:]
                    self.estado = r
                if r.get("status") == "ok":
                    p = r["presidente"]
                    c = {x["n"]: x["nome"] for x in p["candidatos"]}
                    if not p["apurado"]["a"] and not p["apurado"]["b"]:
                        print(f"[{r['atualizado']}] conectado ao TSE · apuração ainda não começou "
                              f"(começa após as 17h do dia 25/10) — para testar agora use o simular.bat")
                        continue_print = False
                    else:
                        continue_print = True
                    if continue_print and self.demo:
                        continue_print = time.time() - getattr(self, "_ult_print", 0) > 10
                        if continue_print:
                            self._ult_print = time.time()
                    if continue_print:
                      print(f"[{r['atualizado']}] {p['pst_soma']:6.2f}% apurado | "
                          f"projeção {c[p['candidatos'][0]['n']]}: {p['projecao']['pct_a']}% | "
                          f"tendência: {c[p['lider']]} ({p['confianca']})")
                else:
                    print(f"[{time.strftime('%H:%M:%S')}] {r.get('mensagem')}")
            except Exception as e:
                with self.lock:
                    self.estado["status_erro"] = f"{time.strftime('%H:%M:%S')}: {e}"
                print(f"  erro na leitura: {e} — tentando de novo em {INTERVALO_SEG}s")
            if self.demo:
                self.acordar.wait(espera)
                self.acordar.clear()
            else:
                time.sleep(espera + random.uniform(0, 2))


# --------------------------------------------------------------------------
# Servidor web local
# --------------------------------------------------------------------------
# ---- quantas pessoas estão com o painel aberto agora ----
_vistos, _vistos_lock, _pico = {}, threading.Lock(), [0]
JANELA_ONLINE = 30  # segundos sem atualizar = saiu


def contar_online(vid):
    agora = time.time()
    with _vistos_lock:
        _vistos[vid] = agora
        for k in [k for k, t in _vistos.items() if agora - t > JANELA_ONLINE]:
            del _vistos[k]
        n = len(_vistos)
        _pico[0] = max(_pico[0], n)
        return n, _pico[0]


def criar_handler(coletor):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def enviar(self, corpo, tipo):
            b = corpo.encode("utf-8") if isinstance(corpo, str) else corpo
            self.send_response(200)
            self.send_header("Content-Type", tipo)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path.startswith("/api/estado"):
                from urllib.parse import urlparse, parse_qs
                vid = (parse_qs(urlparse(self.path).query).get("v", [""])[0] or "")[:40]
                online, pico = contar_online(vid or self.client_address[0])
                with coletor.lock:
                    r = dict(coletor.estado)
                r["online"], r["pico"] = online, pico
                self.enviar(json.dumps(r, ensure_ascii=False), "application/json; charset=utf-8")
            elif self.path.startswith("/api/controle"):
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                vel = q.get("vel", [None])[0]
                acao = q.get("acao", [None])[0]
                r = coletor.controle(vel=float(vel) if vel not in (None, "") else None, acao=acao)
                self.enviar(json.dumps(r, ensure_ascii=False), "application/json; charset=utf-8")
            elif self.path.startswith("/celular"):
                with open(os.path.join(AQUI, "celular.html"), "rb") as f:
                    self.enviar(f.read(), "text/html; charset=utf-8")
            elif self.path.startswith("/icone.png"):
                with open(os.path.join(AQUI, "icone.png"), "rb") as f:
                    self.enviar(f.read(), "image/png")
            elif self.path.split("?")[0] in ("/", "/index.html", "/painel.html"):
                ua = (self.headers.get("User-Agent") or "").lower()
                celular = any(k in ua for k in ("iphone", "android", "mobile")) and "ipad" not in ua
                if celular and "completo=1" not in self.path:
                    self.send_response(302)
                    self.send_header("Location", "/celular")
                    self.end_headers()
                    return
                with open(os.path.join(AQUI, "painel.html"), "rb") as f:
                    self.enviar(f.read(), "text/html; charset=utf-8")
            else:
                self.send_error(404)
    return H


def main():
    ap = argparse.ArgumentParser(description="Painel de apuração 2º turno 2026 (dados do TSE)")
    ap.add_argument("--porta", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 para acessar pelo celular")
    ap.add_argument("--teste-1turno", action="store_true", help="usa os arquivos do 1º turno (para testar antes)")
    ap.add_argument("--eleicao", help="força um código de eleição do TSE (ex.: 6258)")
    ap.add_argument("--demo", action="store_true", help="simulação com dados fictícios, sem internet")
    ap.add_argument("--demo-aleatoria", action="store_true",
                    help="simulação com resultado sorteado (padrão: igual ao 1º turno)")
    ap.add_argument("--demo-minutos", type=float, default=3, help="duração da simulação")
    args = ap.parse_args()

    # Hospedagem (Railway etc.): a plataforma informa a porta em PORT e o modo pode vir em MODO
    if os.environ.get("PORT"):
        args.porta = int(os.environ["PORT"])
        args.host = "0.0.0.0"
    modo = os.environ.get("MODO", "").strip().lower()
    if modo in ("demo", "simulacao", "simulação"):
        args.demo = True
    elif modo in ("teste", "teste1", "teste-1turno", "1turno"):
        args.teste_1turno = True
    if os.environ.get("DEMO_MINUTOS"):
        args.demo_minutos = float(os.environ["DEMO_MINUTOS"])

    # No Windows, "reuse address" deixa dois programas na mesma porta sem erro
    # (o navegador continua vendo o primeiro). Desligado para avisar de verdade.
    ThreadingHTTPServer.allow_reuse_address = sys.platform != "win32"
    try:
        srv = ThreadingHTTPServer((args.host, args.porta), criar_handler(None))
    except OSError:
        print(f"\nERRO: a porta {args.porta} já está em uso — provavelmente o painel já está aberto "
              f"em outra janela preta. Feche a outra janela e rode de novo.\n")
        sys.exit(1)
    col = Coletor(args)
    col.start()
    srv.RequestHandlerClass = criar_handler(col)
    print(f"\nPainel em  http://localhost:{args.porta}   (Ctrl+C para sair)\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrado.")


if __name__ == "__main__":
    main()
