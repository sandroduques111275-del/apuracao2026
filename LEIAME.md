# Painel de apuração — 2º turno 25/10/2026

Lê os arquivos oficiais do TSE (resultados.tse.jus.br) a cada ~45 s e mostra, num painel no navegador,
o apurado e a **projeção do resultado final** de Presidente (por estado e região) e dos Governadores
com 2º turno (AC, AM, DF, ES, RJ, RN, TO).

## Requisitos
Python 3.8 ou mais novo. Nada para instalar (só biblioteca padrão).

## Como usar
```
python3 servidor.py                  # dia da eleição — abre http://localhost:8080
python3 servidor.py --demo           # simulação com dados fictícios (testa o painel sem internet)
python3 servidor.py --teste-1turno   # testa HOJE o acesso ao TSE usando o 1º turno
python3 servidor.py --host 0.0.0.0   # para abrir no celular: http://IP-do-PC:8080
```
No Windows use `python` em vez de `python3`.

Rode o `--teste-1turno` antes de domingo: se aparecerem os números do 1º turno, o acesso ao TSE está ok.
Na primeira execução ele baixa o 1º turno (base da projeção) e guarda em `cache_1turno/`.

No domingo pode deixar rodando desde a tarde: enquanto o TSE não publicar os arquivos do 2º turno o painel
mostra "aguardando" e tenta de novo a cada 2 min (sem martelar o servidor). A votação termina às 17h (Brasília).

## Como a projeção funciona
Os estados apuram em velocidades diferentes (normalmente Sul/Sudeste antes, Norte/Nordeste depois), então o
"apurado" do país no começo da noite engana. Para cada estado o sistema:
1. estima quantos votos válidos ainda faltam (eleitorado das seções não apuradas × taxa de votos válidos do estado);
2. estima o % de cada candidato nesses votos: mistura o que já foi apurado no estado (peso cresce com o % apurado)
   com o desempenho do 1º turno (só os dois finalistas) ajustado pela variação média observada no país;
3. soma tudo e calcula a margem de erro e a chance de vitória.

"Matematicamente definido" = quem está atrás não vira nem com todos os votos que faltam (com folga de 10%).

Em 400 noites simuladas o modelo nunca errou o vencedor quando marcou confiança "alta" ou mais.
Mesmo assim: **é estimativa, não resultado oficial** — o oficial é o do TSE.

## Configuração (topo do servidor.py)
Códigos oficiais do TSE para 2026: Presidente 1º turno 6257 / 2º turno 6258; Estaduais 6259 / 6260.
O sistema também confere o arquivo `ele-c.json` do TSE na partida. Limite do TSE: 100 requisições/s por IP —
o painel faz ~36 a cada 45 s.

## Simulação
- Repete a apuração do 1º turno (só Lula × Flávio), na **ordem real**: cada estado avança no ritmo em que
  as suas urnas chegaram ao TSE em 04/10 (horário de cada seção no arquivo de configuração de seções do TSE).
  Os votos são os totais oficiais de cada estado. Na primeira vez baixa 28 arquivos (um por estado) e guarda em `cache_1turno/`.
- Relógio simulado começa às 17h de 04/10. Botões de velocidade no painel: 1× (tempo real) até 900×, pausar e reiniciar.
- Se o TSE não fornecer os horários, cai no modelo aproximado (avisado no painel).
