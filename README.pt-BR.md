# cc-observatory

Observabilidade local para o Claude Code: veja suas sessões, subagentes, bloqueios, logs,
tokens e custo na sua própria máquina.

[Read in English](README.md)

[![CI](https://github.com/Luanderson-Dev/cc-observatory/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/Luanderson-Dev/cc-observatory/actions/workflows/ci.yml?query=branch%3Adevelop)
[![PyPI](https://img.shields.io/pypi/v/cc-observatory)](https://pypi.org/project/cc-observatory/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)

## Status

**Pré-alfa.** A v0.2.0 fecha o milestone 1 de 8: a stack de observabilidade. O pacote
`cc-observatory` no PyPI ainda só mostra a própria versão; a stack roda a partir de um
clone deste repositório.

O que a v0.2.0 traz:

- a stack de observabilidade no Docker Compose: o OpenTelemetry Collector, o Prometheus,
  o Loki e o Grafana, mais o Tempo no profile opcional `traces`, com todas as portas
  ligadas a `127.0.0.1` (veja "Como rodar a stack de observabilidade" mais abaixo e o
  [docs/stack.md](docs/stack.md), em inglês);
- o Grafana sem login, com as fontes de dados do Prometheus, do Loki e do Tempo e um
  dashboard de visão geral do Claude Code provisionados a partir do repositório;
- os filtros de privacidade do collector, que sempre apagam atributos pessoais e, por
  enquanto, sempre descartam conteúdo (a opção de guardar conteúdo chega na v0.5.0), e um
  rótulo `host` em todo sinal;
- configurações que desligam todos os relatórios de uso e verificações de atualização,
  conferidas por testes com a stack rodando;
- testes de integração no CI que rodam contra uma stack real no compose;
- os ADRs 0013 a 0022, com as decisões da stack.

A v0.1.0 (milestone 0) montou a fundação: o pacote publicado no
[PyPI](https://pypi.org/project/cc-observatory/), o ferramental, o CI, os ADRs 0001 a
0012 e fixtures sanitizadas gravadas a partir de sessões reais do Claude Code.

O que vem a seguir: o milestone 2 (v0.3.0), o app que recebe os hooks do Claude Code e
acompanha sessões, agentes e bloqueios no SQLite. O plano completo está no roteiro mais
abaixo.

Salvo indicação em contrário, o restante deste README descreve o projeto planejado.

## O que ele vai mostrar

Entre parênteses está a release que traz cada parte. Só a parte do Grafana chega com a
v0.2.0 (veja "Como rodar a stack de observabilidade" mais abaixo); o resto está
planejado.

- **Visão geral ao vivo** das sessões do Claude Code na sua máquina: quais estão
  trabalhando, esperando por você, ociosas ou encerradas (v0.4.0).
- **Árvore de agentes**: o agente principal de cada sessão e os subagentes que ele
  iniciou (v0.4.0).
- **Bloqueios**: sessões esperando por você, como um pedido de permissão ou uma
  elicitação de um servidor MCP, e há quanto tempo cada uma está esperando (registrados
  a partir da v0.3.0 e exibidos na interface web a partir da v0.4.0).
- **Eventos e logs** de cada sessão (v0.4.0).
- **Tokens e custo** (v0.4.0) por dia, modelo e tipo de agente (com os nomes dos agentes
  embutidos; os seus próprios agentes aparecem como `custom`, a menos que você ative os
  detalhes de ferramentas) e por projeto (cruzado com o diretório de trabalho que os
  hooks informam). O custo é uma estimativa que vem da telemetria do Claude Code, não o
  valor que você paga.
- **Página de saúde**: se cada parte da stack está no ar e recebendo dados (v0.4.0).
- **Grafana** para análises mais profundas, provisionado com as fontes de dados e um
  dashboard de visão geral do Claude Code (v0.2.0) e, mais tarde, com dashboards que o
  app abre por links já filtrados pela sessão, agente ou projeto que você estava vendo
  (v0.7.0).
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
- **O app** também consulta o Prometheus e o Loki via HTTP para as telas de tokens, custo
  e eventos.
- **O Grafana** já vem provisionado com as fontes de dados do Prometheus, do Loki e do
  Tempo e com os dashboards.
- **Por que as duas fontes:** a telemetria diz o que uma sessão consumiu (tokens, custo,
  chamadas à API, resultados de ferramentas), mas não que ela está esperando por você
  agora, e isso só os hooks informam, no momento em que acontece.

Mais detalhes para quem contribui estão em [docs/architecture.md](docs/architecture.md)
(em inglês). As decisões por trás desse projeto, e os motivos delas, estão nos registros
de decisão de arquitetura em [docs/adr/](docs/adr/README.md) (em inglês).

## Como rodar a stack de observabilidade (a partir da v0.2.0)

A partir da v0.2.0 você pode rodar a stack de observabilidade com um clone deste
repositório e ver as métricas e os eventos do Claude Code no Grafana. A partir da v0.3.0
a stack também roda o app (hooks, sessões e bloqueios; veja "Experimente o app" mais
abaixo). A interface web dele chega na v0.4.0 e o `ccobs setup` na v0.5.0; até lá, você
aponta o Claude Code para a stack manualmente.

1. Suba a stack:

   ~~~bash
   git clone https://github.com/Luanderson-Dev/cc-observatory
   cd cc-observatory
   docker compose up -d
   ~~~

   A primeira subida baixa cerca de 750 MB de imagens e faz o build das imagens do
   collector e do app. O `docker compose ps` mostra cada serviço e se ele está
   saudável. Para guardar também os traces do Claude Code, um recurso beta, suba a stack
   com `docker compose --profile traces up -d`, que acrescenta o Tempo, e passe
   `--profile traces` toda vez que subir ou atualizar a stack.

2. Abra o Grafana em <http://localhost:4330>. Use `localhost`: o Grafana redireciona para
   ele qualquer outro nome de host, inclusive `127.0.0.1`. Não há login. A página inicial
   é o dashboard **Claude Code overview**: custo, tokens, sessões, tempo ativo, mudanças
   de código, erros de API e eventos recentes.

3. Aponte o Claude Code para a stack. Acrescente estas variáveis ao bloco `env` das suas
   configurações de usuário, `~/.claude/settings.json`, mantendo as entradas que você já
   tem, ou exporte-as no shell que inicia o Claude Code. As configurações de projeto
   (`.claude/settings.json` e `.claude/settings.local.json`) não conseguem defini-las.

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
     `OTEL_TRACES_EXPORTER` como `otlp`, e rode a stack com `--profile traces`.

4. Reinicie o Claude Code para que as novas sessões carreguem as configurações. As
   métricas chegam a cada 60 segundos e os eventos a cada 5 segundos, e o dashboard se
   atualiza a cada minuto. Se nada aparecer, veja
   [No data in the dashboard](docs/stack.md#no-data-in-the-dashboard) (em inglês).

- **Configurações.** Toda configuração é uma variável de ambiente com um valor padrão,
  então o arquivo `.env` é opcional: para mudar uma, copie o
  [`.env.example`](.env.example) para `.env`, edite-o e rode `docker compose up -d` de
  novo. `CCOBS_HOST_LABEL` (padrão `local`) é o label `host` guardado em toda métrica,
  evento e trace; defina-o para dar um nome à máquina. Ele é guardado como você
  escrever, então use o nome de host real da máquina só se quiser que ele seja guardado.
  As portas e a retenção (por padrão, 90 dias de métricas, 30 de eventos e 7 de traces)
  são definidas do mesmo jeito.
- **Depois de reiniciar a máquina.** Os serviços reiniciam a menos que você os pare com
  `docker compose stop`, então a stack volta sempre que o Docker inicia
  ([ADR 0019](docs/adr/0019-restart-unless-stopped.md), em inglês). O Docker Desktop só
  inicia quando você entra na sessão se a opção "Start Docker Desktop when you sign in
  to your computer" estiver ligada, e ela vem desligada; no Linux, habilite o serviço do
  Docker na inicialização.
- **Mais.** O [docs/stack.md](docs/stack.md) (em inglês) cobre como parar e atualizar a
  stack, as configurações, os dados e a retenção, a memória e a solução de problemas.

## Experimente o app (a partir da v0.3.0)

A partir da v0.3.0, o `docker compose up -d` também faz o build da imagem do app a
partir do seu clone e o inicia em <http://127.0.0.1:4300>. O app recebe os hooks do
Claude Code e acompanha sessões, agentes e bloqueios no SQLite. A interface web dele
chega na v0.4.0; até lá, você pode ver o que ele acompanha pela API JSON somente
leitura.

1. Suba ou atualize a stack como acima. O `docker compose ps` mostra o `app` como
   saudável assim que ele passa a atender requisições.

2. Aponte o Claude Code para o app. Até o `ccobs setup` gravar os hooks por você na
   v0.5.0, salve o bloco de hooks do
   [docs/app.md](docs/app.md#point-claude-code-at-the-app) (em inglês) como
   `ccobs-hooks.json` no seu diretório pessoal (no Windows, troque `/dev/null` por `NUL`
   nele, como aquela página explica) e inicie o Claude Code com ele:

   ~~~bash
   claude --settings "$HOME/ccobs-hooks.json"
   ~~~

   O seu `~/.claude/settings.json` continua como está, e só as sessões que você iniciar
   assim enviam hooks. A URL dos hooks usa `127.0.0.1`, não `localhost`.

3. Veja as sessões:

   ~~~bash
   curl -s http://127.0.0.1:4300/api/v1/sessions
   ~~~

   Cada sessão traz o estado dela (`working`, `waiting_user`, `idle`, `ended` ou
   `stale`), e `/api/v1/sessions/<session_id>` acrescenta os agentes e os bloqueios
   dela.

- **Stack fora do ar.** Enquanto a stack estiver fora do ar, as sessões iniciadas com
  esses hooks mostram na transcrição um aviso de erro de hook na maioria dos eventos de
  hook; inicie o Claude Code sem `--settings` até a stack voltar.
- **Negações com Esc.** Um pedido de permissão negado com Esc não dispara nenhum hook; o
  app o fecha a partir dos eventos do Claude Code no Loki, então mantenha as
  configurações de telemetria do passo 3 da seção anterior.
- **Mais.** O [docs/app.md](docs/app.md) (em inglês) cobre a API, as métricas do app no
  Grafana, as configurações e a retenção dele, a privacidade e a solução de problemas.

## Início rápido (planejado, disponível a partir da v0.5.0)

**Ainda não disponível.** O passo 1 funciona a partir da v0.2.0 (veja a seção acima);
os passos 2 a 4 precisam do comando `setup`, que chega na v0.5.0.

1. Suba a stack:

   ~~~bash
   git clone https://github.com/Luanderson-Dev/cc-observatory
   cd cc-observatory
   docker compose up -d
   ~~~

2. Conecte o Claude Code a ela:

   ~~~bash
   uvx cc-observatory setup
   ~~~

   O `setup` faz um backup do `~/.claude/settings.json` e mescla nele o bloco `env` da
   telemetria e os hooks sem mexer nas entradas que você já tem, marcando o que
   acrescenta para que o `ccobs uninstall` remova só isso; o `--dry-run` mostra o diff
   sem gravar nada.

3. Abra o app no endereço que o `setup` mostrar.
4. Reinicie o Claude Code para que as novas sessões carreguem as novas configurações.

`ccobs` e `cc-observatory` são a mesma CLI; `uvx cc-observatory <comando>` a executa sem
instalá-la.

## Privacidade

- **Só metadados por padrão.** O texto dos prompts, as entradas e saídas das
  ferramentas, as mensagens do assistente e os textos de erro não são armazenados.
- **A captura de conteúdo é opt-in explícito** (prevista para a v0.5.0).
  O `ccobs setup --capture-content` mostra um aviso e então liga a captura de conteúdo
  nas configurações de telemetria do Claude Code e na stack (no collector e no app). Sem
  ele, o collector descarta o conteúdo da telemetria mesmo que o Claude Code tenha sido
  configurado para enviá-lo.
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
- [uv](https://docs.astral.sh/uv/), para a CLI `ccobs` (`uvx cc-observatory`), cujos
  comandos chegam na v0.5.0.
- Claude Code 2.1.296 ou mais recente. É a versão com que o cc-observatory foi testado
  ([ADR 0012](docs/adr/0012-observed-telemetry-and-hook-contract.md), em inglês);
  versões anteriores não são suportadas até serem testadas.

## Roteiro

| Milestone | Release | Escopo                                                                                           | Status       |
| --------- | ------- | ------------------------------------------------------------------------------------------------ | ------------ |
| 0         | v0.1.0  | Fundação, ferramental, CI, ADRs, spike de telemetria e hooks, fixtures, primeira release no PyPI | Lançado      |
| 1         | v0.2.0  | Stack de observabilidade no compose, Grafana provisionado                                        | Lançado      |
| 2         | v0.3.0  | Ingestão de hooks, máquina de estados da sessão, SQLite, bloqueios, `/metrics`                   | Planejado    |
| 3         | v0.4.0  | Interface web (HTMX + SSE)                                                                       | Planejado    |
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
