# cc-observatory

Observabilidade local para o Claude Code: veja suas sessões, subagentes, bloqueios, logs,
tokens e custo na sua própria máquina.

[Read in English](README.md)

[![CI](https://github.com/Luanderson-Dev/cc-observatory/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/Luanderson-Dev/cc-observatory/actions/workflows/ci.yml?query=branch%3Adevelop)
[![PyPI](https://img.shields.io/pypi/v/cc-observatory)](https://pypi.org/project/cc-observatory/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)

## Status

**Pré-alfa.** A v0.4.0 fecha o milestone 3 de 8: a interface web. O pacote
`cc-observatory` no PyPI ainda só mostra a própria versão; a stack e o app rodam a partir
de um clone deste repositório.

O que a v0.4.0 traz:

- uma interface web em `http://127.0.0.1:4300` (veja "Experimente o app" mais abaixo e
  o [docs/app.md](docs/app.md), em inglês): uma visão geral das sessões agrupadas por
  estado, com há quanto tempo cada uma espera por você, e uma página por sessão com os
  bloqueios dela, a árvore de agentes e a linha do tempo dos hooks;
- atualizações ao vivo por server-sent events, sem recarregar a página;
- tokens e custo por sessão, lidos do Prometheus, e links de cada sessão para os logs,
  tokens e custo dela no Grafana;
- htmx e fontes embutidos no pacote, com checksums e licenças, para a interface
  funcionar offline;
- endurecimento contra o navegador: content security policy estrita, verificação de
  Fetch Metadata e cabeçalhos de resposta defensivos;
- engines de banco separados para o writer e para as leituras, com leituras
  concorrentes limitadas, e páginas limitadas de bloqueios, agentes e eventos na API;
- os ADRs 0031 a 0035, com as decisões da interface.

Até o `ccobs setup` chegar na v0.5.0, você aponta o Claude Code para o app com um arquivo
de settings passado ao `claude --settings`. Releases anteriores: a v0.3.0 (milestone 2)
trouxe o app que recebe os hooks do Claude Code, a v0.2.0 (milestone 1) a stack de
observabilidade no Docker Compose com um Grafana provisionado, e a v0.1.0 (milestone 0)
montou a fundação.

O que vem a seguir: o milestone 4 (v0.5.0), os comandos `ccobs setup`, `uninstall`,
`doctor` e `status`. O plano completo está no roteiro mais abaixo.

Salvo indicação em contrário, o restante deste README descreve o projeto planejado.

## O que ele mostra

Entre parênteses está a release que traz cada parte. As partes até a v0.4.0 rodam a
partir de um clone deste repositório (veja "Como rodar a stack de observabilidade" e
"Experimente o app" mais abaixo); o resto está planejado.

- **Visão geral ao vivo** das sessões do Claude Code na sua máquina, em uma interface
  web em <http://127.0.0.1:4300>: primeiro as sessões esperando por você, com há quanto
  tempo cada uma espera, depois as que estão trabalhando, ociosas, encerradas há pouco
  ou paradas (`stale`). Ela se atualiza enquanto o Claude Code trabalha, sem recarregar
  a página (v0.4.0).
- **Bloqueios**: sessões esperando por você, como um pedido de permissão ou uma
  elicitação de um servidor MCP, com há quanto tempo cada uma está esperando e como
  terminou (registrados a partir da v0.3.0 e exibidos na interface web a partir da
  v0.4.0).
- **Uma página por sessão** (v0.4.0) com os bloqueios dela, a **árvore de agentes** (os
  subagentes, cada um sob o agente que o iniciou), os **tokens e o custo** (tokens por
  tipo, tokens e custo por modelo) e a **linha do tempo dos hooks**, só com metadados.
- **Eventos e logs** de cada sessão: a linha do tempo dos hooks na página dela e um link
  para os eventos do Claude Code dela no Grafana (v0.4.0).
- **Tokens e custo**: por sessão, na página dela (v0.4.0), e somando as sessões, por dia
  e modelo, no dashboard de visão geral do Grafana (v0.2.0); por tipo de agente (com os
  nomes dos agentes embutidos; os seus próprios agentes aparecem como `custom`, a menos
  que você ative os detalhes de ferramentas) e por projeto (cruzado com o diretório de
  trabalho que os hooks informam) estão planejados para a v0.7.0. O custo é uma estimativa que vem da
  telemetria do Claude Code, não o valor que você paga.
- **Página de saúde**: se cada parte da stack está no ar e recebendo dados (planejada, v0.7.0).
- **Grafana** para análises mais profundas, provisionado com as fontes de dados e um
  dashboard de visão geral do Claude Code (v0.2.0); a página de cada sessão leva aos
  logs, tokens e custo dela lá (v0.4.0) e, mais tarde, dashboards vão abrir já filtrados
  pelo agente ou projeto que você estava vendo (v0.7.0).
- **Alertas** (v0.6.0): notificações no navegador e, opcionalmente, um webhook do Discord
  quando uma sessão espera por você além de um limite de tempo, quando erros de API se
  repetem ou quando o custo estimado de uma sessão ou de um dia passa de um limite. Você
  define os limites por variáveis de ambiente ou em uma tela de configurações.

## Como funciona

~~~text
Claude Code ──OTLP (4317/4318)──► OTel Collector ──► Prometheus (metrics)
     │                                         ├──► Loki (events/logs)
     │                                         └──► Tempo (traces, optional profile)
     └──hooks──► app (FastAPI) ──► SQLite (sessions, agents, blocks, alerts)
                    │  └─ /metrics ◄── scraped by Prometheus (live state)
                    └─ queries Prometheus and Loki over HTTP
Grafana ◄── Prometheus, Loki, Tempo (provisioned datasources and dashboards)
~~~

- **Telemetria.** O suporte nativo do Claude Code ao OpenTelemetry exporta métricas,
  eventos e, opcionalmente, traces via OTLP para um OpenTelemetry Collector. O collector
  remove os atributos pessoais e envia as métricas para o Prometheus, os eventos para o
  Loki e os traces para o Tempo. O Tempo só roda em um profile opcional, `traces`,
  porque o tracing ainda é um recurso beta no Claude Code.
- **Hooks.** Os hooks do Claude Code enviam eventos do ciclo de vida (um prompt enviado,
  um pedido de permissão, uma notificação, um subagente que começa ou termina, uma
  compactação, o fim de uma sessão) para o app, um serviço FastAPI que guarda sessões,
  agentes, bloqueios e alertas no SQLite.
- **Estado ao vivo como métricas.** O app expõe esse estado ao vivo em `/metrics`, que o
  Prometheus coleta, para que ele apareça nos gráficos ao lado da telemetria.
- **A interface web.** O app serve as próprias páginas, que acompanham as sessões ao
  vivo por server-sent events, e lê os tokens e o custo de cada sessão no Prometheus.
  Ele também fecha os pedidos de permissão negados com Esc a partir dos eventos do
  Claude Code no Loki.
- **O Grafana** já vem provisionado com as fontes de dados do Prometheus, do Loki e do
  Tempo e com os dashboards.
- **Por que as duas fontes:** a telemetria diz o que uma sessão consumiu (tokens, custo,
  chamadas à API, resultados de ferramentas), mas não que ela está esperando por você
  agora, e isso só os hooks informam, no momento em que acontece.

Mais detalhes para quem contribui estão em [docs/architecture.md](docs/architecture.md)
(em inglês). As decisões por trás desse projeto, e os motivos delas, estão nos registros
de decisão de arquitetura em [docs/adr/](docs/adr/README.md) (em inglês).

## Início rápido (a partir da v0.5.0)

1. Suba a stack:

   ~~~bash
   git clone https://github.com/Luanderson-Dev/cc-observatory
   cd cc-observatory
   docker compose up -d
   ~~~

   A primeira subida baixa cerca de 750 MB de imagens e faz o build das imagens do
   collector e do app. O `docker compose ps` mostra cada serviço e se ele está saudável.

2. Conecte o Claude Code a ela:

   ~~~bash
   uvx cc-observatory setup
   ~~~

   O `setup` faz um backup das suas configurações de usuário (`~/.claude/settings.json`)
   e acrescenta a elas os hooks e o bloco `env` da telemetria, mantendo as suas próprias
   entradas. Ele registra o que acrescentou, para que o `ccobs uninstall` remova só isso;
   o `--dry-run` mostra o diff sem gravar nada, e rodá-lo de novo é seguro.

3. Inicie uma nova sessão do Claude Code. Os hooks chegam na hora às sessões já
   abertas, mas a telemetria (tokens, custo, eventos) só começa nas sessões iniciadas
   depois do `setup`; então reinicie as que você quiser ver por completo.

4. Abra o app em <http://127.0.0.1:4300>, o endereço que o `setup` mostra, e o Grafana
   em <http://localhost:4330>.

Se nada aparecer, rode `uvx cc-observatory doctor`: ele confere o Claude Code, as suas
configurações, as políticas que podem bloquear os hooks ou a telemetria e a stack, e diz
como corrigir o que encontrar. O `uvx cc-observatory status` é a versão curta.

- **A CLI.** `ccobs` e `cc-observatory` são a mesma CLI: `uvx cc-observatory <comando>`
  a executa sem instalá-la, e `uv tool install cc-observatory` a instala como `ccobs`. O
  [docs/cli.md](docs/cli.md) (em inglês) cobre o `setup`, o `uninstall`, o `doctor` e o
  `status`, as opções e os códigos de saída deles e a solução de problemas.
- **Sem mudar as suas configurações.** Para conectar só uma sessão, inicie o Claude Code
  com os hooks num arquivo próprio, `claude --settings <arquivo>`, como o
  [docs/app.md](docs/app.md#point-claude-code-at-the-app) (em inglês) mostra.
- **Desfazer.** O `uvx cc-observatory uninstall` tira o que o `setup` acrescentou e
  devolve qualquer valor que ele tenha substituído. A stack continua rodando até um
  `docker compose down`.

## Como rodar a stack de observabilidade (a partir da v0.2.0)

A partir da v0.2.0 você pode rodar a stack de observabilidade com um clone deste
repositório e ver as métricas e os eventos do Claude Code no Grafana. A partir da v0.3.0
a stack também roda o app (hooks, sessões e bloqueios; veja "Experimente o app" mais
abaixo) e, a partir da v0.4.0, a interface web dele.

1. Suba a stack como no início rápido. Para guardar também os traces do Claude Code, um
   recurso beta, suba a stack com `docker compose --profile traces up -d`, que
   acrescenta o Tempo, e passe `--profile traces` toda vez que subir ou atualizar a
   stack.

2. Abra o Grafana em <http://localhost:4330>. Use `localhost`: o Grafana redireciona para
   ele qualquer outro nome de host, inclusive `127.0.0.1`. Não há login. A página inicial
   é o dashboard **Claude Code overview**: custo, tokens, sessões, tempo ativo, mudanças
   de código, erros de API e eventos recentes.

3. Aponte o Claude Code para a stack. O `ccobs setup` grava estas variáveis no bloco
   `env` das suas configurações de usuário, `~/.claude/settings.json` (o `--traces`
   acrescenta as duas variáveis de traces abaixo, o `--otlp-port` muda a porta). Para
   defini-las manualmente, acrescente-as a esse bloco, mantendo as entradas que você já
   tem, ou exporte-as no shell que inicia o Claude Code. As configurações de projeto
   (`.claude/settings.json` e `.claude/settings.local.json`) não conseguem defini-las.

   <!-- telemetry-env: telemetry_env(), kept equal by tests/test_docs_snippet.py -->

   ~~~json
   {
     "env": {
       "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
       "OTEL_METRICS_EXPORTER": "otlp",
       "OTEL_LOGS_EXPORTER": "otlp",
       "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
       "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
       "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "cumulative"
     }
   }
   ~~~

   - Use `127.0.0.1`, não `localhost`: as portas escutam só em IPv4, e `localhost` pode
     resolver para `::1`.
   - O Claude Code não tem protocolo padrão, então defina um. `grpc` também funciona, com
     `http://127.0.0.1:4317`.
   - A temporalidade cumulativa é a recomendada: com o padrão do Claude Code, delta,
     alguns totais podem sair menores depois de uma pausa
     ([o limite conhecido](docs/stack.md#known-limit-delta-temporality), em inglês).
   - Deixe `OTEL_METRICS_INCLUDE_SESSION_ID` no padrão (`true`): sem os IDs de sessão, as
     séries de sessões simultâneas colidem.
   - Para traces, defina também `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA` como `1` e
     `OTEL_TRACES_EXPORTER` como `otlp` (ou rode `ccobs setup --traces`), e rode a stack
     com `--profile traces`.

4. Reinicie o Claude Code para que as novas sessões carreguem as configurações. As
   métricas chegam a cada 60 segundos e os eventos a cada 5 segundos, e o dashboard se
   atualiza a cada minuto. Se nada aparecer, rode `ccobs doctor` ou veja
   [No data in the dashboard](docs/stack.md#no-data-in-the-dashboard) (em inglês).

- **Configurações.** Toda configuração é uma variável de ambiente com um valor padrão,
  então o arquivo `.env` é opcional: para mudar uma, copie o
  [`.env.example`](.env.example) para `.env`, edite-o e rode `docker compose up -d` de
  novo. `CCOBS_HOST_LABEL` (padrão `local`) é o label `host` guardado em toda métrica,
  evento e trace; defina-o para dar um nome à máquina. Ele é guardado como você
  escrever, então use o nome de host real da máquina só se quiser que ele seja guardado.
  As portas e a retenção (por padrão, 90 dias de métricas, 30 de eventos e 7 de traces)
  são definidas do mesmo jeito. Se você mudar uma porta, passe a mesma porta ao `ccobs`
  (`--app-port`, `--otlp-port`, `--grafana-port`).
- **Depois de reiniciar a máquina.** Os serviços reiniciam a menos que você os pare com
  `docker compose stop`, então a stack volta sempre que o Docker inicia
  ([ADR 0019](docs/adr/0019-restart-unless-stopped.md), em inglês). O Docker Desktop só
  inicia quando você entra na sessão se a opção "Start Docker Desktop when you sign in
  to your computer" estiver ligada, e ela vem desligada; no Linux, habilite o serviço do
  Docker na inicialização.
- **Mais.** O [docs/stack.md](docs/stack.md) (em inglês) cobre como parar e atualizar a
  stack, as configurações, os dados e a retenção, a memória e a solução de problemas.

## Experimente o app (a partir da v0.3.0, interface web a partir da v0.4.0)

A partir da v0.3.0, o `docker compose up -d` também faz o build da imagem do app a
partir do seu clone e o inicia em <http://127.0.0.1:4300>. O app recebe os hooks do
Claude Code e acompanha sessões, agentes e bloqueios no SQLite. A partir da v0.4.0 ele
os mostra em uma interface web nesse endereço; ele também os serve por uma API JSON
somente leitura.

1. Suba ou atualize a stack como acima. O `docker compose ps` mostra o `app` como
   saudável assim que ele passa a atender requisições.

2. Aponte o Claude Code para o app: o `ccobs setup` grava os hooks (veja o início
   rápido). A URL dos hooks usa `127.0.0.1`, não `localhost`. Para enviar os hooks de
   uma sessão só, sem mudar as suas configurações, use o arquivo de hooks e o
   `claude --settings` do [docs/app.md](docs/app.md#point-claude-code-at-the-app) (em
   inglês).

3. Abra <http://127.0.0.1:4300> no navegador. A visão geral mostra as suas sessões em
   faixas por estado, primeiro as que esperam por você, e as acompanha ao vivo; clique
   numa sessão para ver os bloqueios, os agentes, os tokens e o custo e a linha do tempo
   dos hooks dela, e os links para os logs e o uso dela no Grafana. Até a primeira
   sessão chegar, a visão geral mostra o comando `ccobs setup` para a porta dela. Os
   mesmos dados estão na API JSON:

   ~~~bash
   curl -s http://127.0.0.1:4300/api/v1/sessions
   ~~~

   Cada sessão traz o estado dela (`working`, `waiting_user`, `idle`, `ended` ou
   `stale`), e `/api/v1/sessions/<session_id>` acrescenta os agentes e os bloqueios
   dela.

- **Stack fora do ar.** Enquanto a stack estiver fora do ar, o Claude Code mostra na
  transcrição um aviso de erro de hook na maioria dos eventos de hook; suba a stack, ou
  rode `ccobs uninstall` para tirar os hooks até ela voltar.
- **Negações com Esc, tokens e custo.** Um pedido de permissão negado com Esc não
  dispara nenhum hook; o app o fecha a partir dos eventos do Claude Code no Loki. Os
  tokens e o custo de uma sessão vêm das métricas do Claude Code no Prometheus. Os dois
  precisam das configurações de telemetria que o `ccobs setup` grava (passo 3 da seção
  anterior).
- **Mais.** O [docs/app.md](docs/app.md) (em inglês) cobre a interface web, a API, as
  métricas do app no Grafana, as configurações e a retenção dele, a privacidade e a
  solução de problemas.

## Privacidade

- **Só metadados por padrão.** O texto dos prompts, as entradas e saídas das
  ferramentas, as mensagens do assistente e os textos de erro não são armazenados.
- **Ainda sem captura de conteúdo.** O `ccobs setup` nunca liga a captura de conteúdo,
  e o collector descarta o conteúdo da telemetria mesmo que o Claude Code tenha sido
  configurado para enviá-lo; o `ccobs doctor` avisa quando as variáveis de conteúdo do
  Claude Code estão ligadas. O opt-in explícito para guardar conteúdo foi adiado para um
  release futuro ([ADR 0040](docs/adr/0040-defer-the-capture-content-opt-in.md), em
  inglês).
- **Os atributos pessoais são removidos.** O collector apaga da telemetria o seu e-mail,
  os IDs do seu usuário, da sua conta e da sua organização e o nome de host da sua
  máquina antes que qualquer coisa seja armazenada.
- **Tudo fica na sua máquina.** Esse é um objetivo do projeto que as configurações
  incluídas garantem: todas as portas escutam só em `127.0.0.1`; o Prometheus, o Loki e
  o Tempo não publicam porta nenhuma e ficam numa rede interna, sem rota para fora; e o
  envio de dados de uso e a busca por atualizações do Grafana, do Loki e do Tempo ficam
  desligados. Durante o uso, nada sai da sua máquina, exceto o webhook opcional do
  Discord, se você configurar um, e as consultas de DNS pelo nome de um armazenamento
  enquanto ele estiver parado (veja o [SECURITY.md](SECURITY.md)).
- **A interface web** (a partir da v0.4.0) também não tem login, não carrega nada da
  internet e não muda nada. Ela mostra o diretório de trabalho só como o nome do
  projeto, ou com o seu diretório pessoal como `~`, e recusa as requisições que outras
  páginas web fazem pelo seu navegador (um link para ela continua só abrindo a
  página).
- **O Grafana não tem login.** Quem o abre é um Editor, que pode consultar os dados e
  salvar dashboards, mas não pode mudar as fontes de dados, os plugins, os usuários nem
  as configurações dele. Ele só responde como `localhost`, o que impede outras páginas
  web de lê-lo por DNS rebinding, e recusa as requisições de outros sites que poderiam
  alterá-lo ou ler as respostas dele; uma página ainda consegue fazê-lo rodar uma
  consulta às cegas, sem ver o resultado. Qualquer programa que rode na sua máquina pode
  ler os dados.
- **A retenção é configurável** (na stack de observabilidade a partir da v0.2.0, por
  padrão 90 dias de métricas, 30 de eventos e 7 de traces; no banco do app a partir da
  v0.3.0), então os dados antigos são apagados depois do período que você escolher.

Veja o [SECURITY.md](SECURITY.md) (em inglês) para o modelo de segurança, inclusive o que
uma página web ainda consegue fazer, e para saber como relatar uma vulnerabilidade.

## Sem interferência no Claude Code

- **Os hooks nunca mudam as decisões do Claude Code.** O app responde a toda chamada de
  hook com uma resposta 2xx vazia, nunca com uma decisão, e o `setup` dá a todo hook um
  timeout curto.
- **Se a stack estiver fora do ar, o Claude Code continua funcionando.** Enquanto a stack
  estiver fora do ar, o Claude Code mostra na transcrição um aviso de erro de hook na
  maioria dos eventos de hook (o próprio Claude não vê esses avisos). O
  [ADR 0011](docs/adr/0011-hook-delivery-http-plus-async-sessionstart.md) (em inglês)
  explica esse trade-off e como o cc-observatory o mantém pequeno: o `ccobs doctor`
  confere se o endpoint dos hooks responde e aponta os hooks que estão falhando, e o
  `ccobs uninstall` remove os hooks.

## Requisitos

- Docker Engine 25.0 ou mais recente com o plugin Docker Compose 2.20.2 ou mais recente
  (e, se o plugin Buildx estiver instalado, Buildx 0.17.0 ou mais recente): o Docker
  Desktop no Windows e no macOS, ou os pacotes do próprio Docker no Linux, onde o Docker
  Engine 28.0 ou mais recente também impede que outros hosts da rede local alcancem as
  portas publicadas em `127.0.0.1`.
- Memória: deixe no Docker pelo menos 2304 MiB para a stack (2816 MiB com o Tempo), a
  soma dos limites de memória dela. O uso medido foi de cerca de 260 a 290 MiB em
  repouso e de 420 a 450 MiB enquanto as consultas do dashboard rodavam pela API do
  Grafana, antes de o app entrar na stack; o app, medido sozinho, usou cerca de 64 a 70
  MiB. Carregar a interface do Grafana no navegador levou, por pouco tempo, só o Grafana
  a cerca de 630 MiB.
- Disco: cerca de 3 GB para as imagens, mais cerca de 250 MB para a imagem do app, mais
  os dados.
- Testado no Linux e no Windows 11 com o Docker Desktop, ambos em amd64; macOS e arm64
  não foram testados. Os detalhes estão em
  [docs/stack.md](docs/stack.md#requirements) (em inglês).
- [uv](https://docs.astral.sh/uv/), para a CLI `ccobs` (`uvx cc-observatory`; veja o
  [docs/cli.md](docs/cli.md), em inglês).
- Claude Code 2.1.296 ou mais recente. É a versão com que o cc-observatory foi testado
  ([ADR 0012](docs/adr/0012-observed-telemetry-and-hook-contract.md), em inglês);
  versões anteriores não são suportadas até serem testadas.

## Roteiro

| Milestone | Release | Escopo                                                                                           | Status       |
| --------- | ------- | ------------------------------------------------------------------------------------------------ | ------------ |
| 0         | v0.1.0  | Fundação, ferramental, CI, ADRs, spike de telemetria e hooks, fixtures, primeira release no PyPI | Lançado      |
| 1         | v0.2.0  | Stack de observabilidade no compose, Grafana provisionado                                        | Lançado      |
| 2         | v0.3.0  | Ingestão de hooks, máquina de estados da sessão, SQLite, bloqueios, `/metrics`                   | Lançado      |
| 3         | v0.4.0  | Interface web (HTMX + SSE)                                                                       | Lançado      |
| 4         | v0.5.0  | `ccobs setup`, `uninstall`, `doctor`, `status`                                                   | Planejado    |
| 5         | v0.6.0  | Alertas (navegador e Discord)                                                                    | Planejado    |
| 6         | v0.7.0  | Dashboards do Grafana e acabamento                                                               | Planejado    |
| 7         | v1.0.0  | Distribuição: imagens multi-arch no GHCR, endurecimento das releases, docs para contribuidores   | Planejado    |

## Desenvolvimento

Você precisa do [uv](https://docs.astral.sh/uv/); ele instala a versão certa do Python
para você.

~~~bash
uv sync                                # cria o virtualenv com as dependências de dev
uv run pre-commit install              # instala os hooks de pre-commit e commit-msg
uv run pytest                          # testes unitários com a cobertura mínima
uv run mypy                            # checagem de tipos em modo estrito
uv run pre-commit run --all-files      # todas as checagens do job de lint do CI
uv run pytest -m integration --no-cov  # testes de integração (precisam do Docker)
~~~

- Os testes de integração sobem um projeto compose próprio, `ccobs-it`, em portas
  próprias, então podem rodar ao lado da sua stack; veja
  [docs/stack.md](docs/stack.md#running-the-integration-tests) (em inglês).
- O projeto usa Gitflow. `develop` é a branch padrão: os pull requests de feature têm
  `develop` como destino e são integrados com squash, e os pull requests de release vão
  para a `main` com merge commit.
- O título de um pull request de feature vira o commit dele na `develop` (e, para `feat`,
  `fix`, `refactor` e `perf`, uma linha no changelog), então todo título de PR precisa
  ser um
  [Conventional Commit](https://www.conventionalcommits.org/) (por exemplo,
  `feat: add the session state machine`). O CI confere isso.
- Um guia para contribuidores chega com a v1.0.0.

## Aviso legal

O cc-observatory é um projeto open source independente. Ele não é afiliado à Anthropic,
nem endossado ou patrocinado por ela. "Claude" e "Claude Code" são marcas comerciais da
Anthropic, PBC.

## Licença

[MIT](LICENSE). Copyright (c) 2026 Luanderson Pimenta Mendes (Luanderson-Dev).
