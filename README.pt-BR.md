# cc-observatory

Observabilidade local para o Claude Code: veja suas sessões, subagentes, bloqueios, logs,
tokens e custo na sua própria máquina.

[Read in English](README.md)

[![CI](https://github.com/Luanderson-Dev/cc-observatory/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/Luanderson-Dev/cc-observatory/actions/workflows/ci.yml?query=branch%3Adevelop)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)

## Status

**Pré-alfa.** O cc-observatory está no milestone 0 de 8 (fundação). Ainda não há nada
útil para instalar.

O que já existe:

- a fundação do repositório: o pacote Python `cc-observatory` (pacote de importação
  `ccobs`) com uma CLI `ccobs` que só mostra a própria versão;
- o ferramental: uv, ruff, mypy em modo estrito, pytest com cobertura mínima, pre-commit
  e Commitizen;
- CI no GitHub Actions: lint, checagem de tipos e testes em Linux, macOS e Windows, build
  do pacote, checagem de Conventional Commits nos títulos de PR e um workflow de release
  que publica no PyPI por trusted publishing;
- registros de decisão de arquitetura (ADRs) para as decisões tomadas até agora,
  incluindo como os hooks chegam ao app
  ([ADR 0011](docs/adr/0011-hook-delivery-http-plus-async-sessionstart.md), em inglês)
  e o que o Claude Code 2.1.296 realmente envia na telemetria e nos hooks
  ([ADR 0012](docs/adr/0012-observed-telemetry-and-hook-contract.md), em inglês);
- o ferramental de captura de fixtures para desenvolvimento (`tools/capture/`, fora do
  pacote) e fixtures de teste sanitizadas, gravadas a partir de sessões reais e isoladas
  do Claude Code (`tests/fixtures/`).

O que vem a seguir: a primeira release no PyPI (v0.1.0), que fecha o milestone 0, e,
depois, a stack de observabilidade no Docker Compose com um Grafana provisionado
(v0.2.0). O plano completo está no roteiro mais abaixo.

Salvo indicação em contrário, o restante deste README descreve o projeto planejado.

## O que ele vai mostrar

Nada disso funciona ainda. Entre parênteses está a release em que cada parte está
prevista.

- **Visão geral ao vivo** das sessões do Claude Code na sua máquina: quais estão
  trabalhando, esperando por você, ociosas ou encerradas (v0.4.0).
- **Árvore de agentes**: o agente principal de cada sessão e os subagentes que ele
  iniciou (v0.4.0).
- **Bloqueios**: sessões esperando por você, como um pedido de permissão, um aviso de
  ociosidade, uma elicitação de um servidor MCP ou uma requisição à API que falhou, e há
  quanto tempo cada uma está esperando (registrados a partir da v0.3.0 e exibidos na
  interface web a partir da v0.4.0).
- **Eventos e logs** de cada sessão (v0.4.0).
- **Tokens e custo** (v0.4.0) por dia, modelo e tipo de agente (com os nomes dos agentes
  embutidos; os seus próprios agentes aparecem como `custom`, a menos que você ative os
  detalhes de ferramentas) e por projeto (cruzado com o diretório de trabalho que os
  hooks informam). O custo é uma estimativa que vem da telemetria do Claude Code, não o
  valor que você paga.
- **Página de saúde**: se cada parte da stack está no ar e recebendo dados (v0.4.0).
- **Grafana** para análises mais profundas, provisionado com as fontes de dados (v0.2.0)
  e com dashboards que o app abre por links já filtrados pela sessão, agente ou projeto
  que você estava vendo (v0.7.0).
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

## Início rápido (planejado, disponível a partir da v0.5.0)

**Estes passos ainda não funcionam.** A stack chega na v0.2.0 e o comando `setup`, na
v0.5.0.

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
- **A captura de conteúdo é opt-in explícito.** O `ccobs setup --capture-content` mostra
  um aviso e então liga a captura de conteúdo nas configurações de telemetria do Claude
  Code e na stack (no collector e no app). Sem ele, o collector descarta o conteúdo da
  telemetria mesmo que o Claude Code tenha sido configurado para enviá-lo.
- **Os atributos pessoais são removidos.** O collector apaga da telemetria o seu e-mail e
  os IDs do seu usuário, da sua conta e da sua organização antes que qualquer coisa seja
  armazenada.
- **Tudo fica na sua máquina.** Esse é um objetivo do projeto que as configurações
  incluídas garantem: todas as portas escutam só em `127.0.0.1`, e o envio de dados de
  uso e a busca por atualizações do Grafana, do Loki e do Tempo ficam desligados. Durante
  o uso, nada sai da sua máquina, exceto o webhook opcional do Discord, e só se você
  configurar um.
- **A retenção é configurável** (na stack de observabilidade a partir da v0.2.0, no banco
  do app a partir da v0.3.0), então os dados antigos são apagados depois do período que
  você escolher.

Veja o [SECURITY.md](SECURITY.md) (em inglês) para o modelo de segurança e para saber como
relatar uma vulnerabilidade.

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

## Requisitos (planejado)

- Docker com Compose v2 (Docker Desktop no Windows e no macOS, ou Docker Engine com o
  plugin Compose no Linux).
- [uv](https://docs.astral.sh/uv/).
- Claude Code 2.1.296 ou mais recente. É a versão com que o cc-observatory foi testado
  ([ADR 0012](docs/adr/0012-observed-telemetry-and-hook-contract.md), em inglês);
  versões anteriores não são suportadas até serem testadas.

## Roteiro

| Milestone | Release | Escopo                                                                                           | Status       |
| --------- | ------- | ------------------------------------------------------------------------------------------------ | ------------ |
| 0         | v0.1.0  | Fundação, ferramental, CI, ADRs, spike de telemetria e hooks, fixtures, primeira release no PyPI | Em andamento |
| 1         | v0.2.0  | Stack de observabilidade no compose, Grafana provisionado                                        | Planejado    |
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
uv sync                            # cria o virtualenv com as dependências de dev
uv run pre-commit install          # instala os hooks de pre-commit e commit-msg
uv run pytest                      # testes unitários com a cobertura mínima
uv run mypy                        # checagem de tipos em modo estrito
uv run pre-commit run --all-files  # todas as checagens do job de lint do CI
~~~

- O projeto usa Gitflow. `develop` é a branch padrão: os pull requests de feature têm
  `develop` como destino e são integrados com squash, e os pull requests de release vão
  para a `main` com merge commit.
- O título de um pull request de feature vira a linha dele no changelog, então todo
  título de PR precisa ser um
  [Conventional Commit](https://www.conventionalcommits.org/) (por exemplo,
  `feat: add the session state machine`). O CI confere isso.
- Um guia para contribuidores chega com a v1.0.0.

## Aviso legal

O cc-observatory é um projeto open source independente. Ele não é afiliado à Anthropic,
nem endossado ou patrocinado por ela. "Claude" e "Claude Code" são marcas comerciais da
Anthropic, PBC.

## Licença

[MIT](LICENSE). Copyright (c) 2026 Luanderson Pimenta Mendes (Luanderson-Dev).
