# Plano: medição de custo, menos desperdício e GPT-6 Luna como executor

Objetivo: saber quanto cada execução custa em tokens e dinheiro, cortar o tempo perdido em falhas evitáveis e permitir trocar o executor (hoje Gemini via `agy`) pelo GPT-6 Luna, mantendo o Claude como arquiteto (plano de assinatura, sem API).

## Ponto de partida

Dados das 10 execuções no Sanagua, de 17 a 18/11/2026:

- **Tempo e desperdício:** ~7,1 h de executor, das quais 40–50% foram gastas em tarefas que falharam, foram canceladas ou acabaram revertidas.
- **Sem medição de tokens:** o `status.json` registra apenas o tempo de cada fase, então qualquer custo hoje é estimativa.
- **Luna fora do `agy`:** a lista de `agy models` não inclui o GPT-6 Luna, então é preciso um executor novo.
- **Revisor:** o revisor (`claude -p --model sonnet`) também consome o limite do plano.
- **Instalação editável:** o pacote está instalado em modo editável a partir deste repositório, então uma execução do Contracanto sobre o próprio Contracanto alteraria o worker em uso. As fases abaixo devem ser implementadas sem o próprio Contracanto, ou com um clone separado instalado em outra `.venv`.

Motivos registrados das falhas:

| Execução | Tarefa | Motivo | Categoria |
|---|---|---|---|
| ee33 | T01 | Executor rodou 15 min sem alterar arquivos | executor parado |
| d30c, b330 | — | Worker encerrado sem registrar o estado final (1–2 s) | ferramenta |
| c1b9 | T05a | `[WinError 2]`: executável não encontrado (28 s) | ferramenta / pré-checagem |
| 6cda | T02 | Patch de 1,6 milhão de caracteres, acima do limite de revisão | tarefa grande demais |
| 3ec4 | T05a | Critério `npm run check` não comprovado: o run usou `npm test` | critério × comando de teste |
| 5c89, 0cdb | T01, T10 | Testes falharam na integração depois da aprovação | integração |
| a84f | T05 | Rejeitado após dois reparos | tarefa grande demais |

Alguns desses casos já foram atacados depois: `ec50a13` faz a base rodar o comando de teste resolvido, `ea5d1ad` prepara a raiz antes da integração, e `CONTRACANTO_TEST_COMMAND` agora fixa `npm run check` no `.mcp.json` do Sanagua. As métricas das fases 1 e 3 vão mostrar se esses casos voltam a acontecer.

## Ordem e dependências

```
F0 verificação ─┬─> F1 tokens ─> F2 custo ─────────┐
                └─> F5 executor plugável + Luna ───┤
F3 categorias de falha ─> F4 fail-fast/watchdog ───┼─> F7 piloto comparativo
F6 regras de tamanho de tarefa ────────────────────┘
```

F3 e F6 não dependem de F0 e podem começar já. Cada fase vira commits próprios, com os estruturais separados dos comportamentais, e cada uma atualiza `README.md` e `docs/recuperacao.md` quando muda campos do `status.json` ou das ferramentas MCP.

---

## F0: verificação (spike, sem código de produção)

1. Ver se o evento `result` do `agy --output-format stream-json` traz contagem de tokens. Guardar uma saída real, sem dados sensíveis, como fixture em `tests/`.
2. Confirmar os campos `usage` e `total_cost_usd` do `claude -p --output-format json`. O custo que ele informa é o equivalente de API, não uma cobrança, porque o revisor roda no plano.
3. Instalar o Codex CLI e confirmar três pontos: `codex exec --json` com o modelo `gpt-6-luna`, o formato dos eventos JSONL (a mensagem final e o `usage` por turno, com tokens em cache) e o comportamento de `--sandbox workspace-write`, que precisa bloquear a rede.
4. Confirmar o preço do Luna na página oficial da OpenAI. Os valores de referência abaixo vêm de agregadores.

**Pronto quando:** existirem fixtures reais de saída de `agy`, `claude` e `codex`, e uma nota curta dizendo quais campos de tokens cada um oferece.

### Resultado da F0 (19/11/2026)

Feita com uma tarefa mínima (corrigir `add`, que subtraía) em repositórios descartáveis. As fixtures, sem caminhos locais nem identificadores, ficam em `tests/fixtures/agents/`: `agy-stream.jsonl`, `claude-review.json` e `codex-exec.jsonl`.

**Campos de tokens de cada CLI:**

| CLI | Onde aparece | Campos | `input` inclui o cache? |
|---|---|---|---|
| `agy` (stream-json) | `result.usage` (total) e `step_update.usage` (por passo; a soma bate com o total) | `input_tokens`, `cache_read_tokens`, `output_tokens`, `thinking_tokens`, `total_tokens` | **Não**: `cache_read_tokens` vem à parte, e `total_tokens` = input + output, sem o raciocínio |
| `claude -p` (json) | `usage`, `modelUsage[modelo]`, `total_cost_usd` | `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`; `costBasis: "list"` | Não. O `total_cost_usd` é preço de tabela, um equivalente informativo quando a conta é de assinatura |
| `codex exec --json` | evento `turn.completed.usage`, um por turno | `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens` | **Sim**: o custo sem cache é `input_tokens - cached_input_tokens` |

A F1 precisa normalizar os três para um formato único: `input` sem cache, `cached_input`, `cache_write`, `output` e `reasoning`.

**Medições:**

| Execução | Entrada (em cache) | Saída | Equivalente de API |
|---|---|---|---|
| `agy` + `gemini-4-hydrogen-high` | 20.780 + 37.372 lidos do cache | 1.061 (+229 de raciocínio) | — |
| `codex` + `gpt-6-luna`, com a configuração do usuário | 118.190 (94.464) | 617 | ~US$ 0,0036 |
| `codex` + `gpt-6-luna`, configuração limpa | 64.286 (55.040) | 314 | ~US$ 0,0016 |
| `claude -p --model sonnet` (revisão mínima) | 3.448, quase tudo gravação de cache | 35 | US$ 0,0116 (informativo) |

**Achados que mudam o plano:**

1. **O Codex está logado com a conta do ChatGPT**, não com chave de API. Assim, o uso do Luna consome o limite do plano ChatGPT e **não gera cobrança por token**. A estimativa em dólar da F2 só vale para uma chave de API (`codex login --with-api-key`, feita pelo responsável). Com o login atual, ela é um equivalente informativo, como no `claude`. A F2 deve registrar `billing` por executor a partir de `codex login status`.
2. **A configuração do usuário (`~/.codex`) quase dobra o contexto** (de 64k para 118k na primeira chamada) por causa de plugins e skills. O executor deve rodar com `--ignore-user-config`, e então precisa passar explicitamente:
   - `-c approval_policy="never"`, porque sem isso todo comando é rejeitado ("blocked by policy") e o agente não faz nada, o que reproduziria o caso `executor_noop`;
   - `-c windows.sandbox="elevated"`, porque sem isso o workspace fica só para leitura no Windows.
3. **Sandbox `workspace-write` (com `elevated`):**
   - **Rede:** bloqueada (`Invoke-WebRequest` respondeu "Unable to connect to the remote server").
   - **Escrita fora do worktree:** negada (`UnauthorizedAccessException`).
   - **Exceção:** `%TEMP%` continua gravável, então um executor ainda poderia escrever em arquivos temporários de outros processos, mas não nos outros worktrees, que ficam em `%LOCALAPPDATA%\Contracanto\worktrees`. Para fechar isso, avaliar `-c sandbox_workspace_write.exclude_tmpdir_env_var=true` junto com o `npm ci`/`npm run check` do Sanagua, que podem depender de temporários.
4. **O PATH não se atualiza nos processos já abertos.** O `codex.exe` foi instalado em `%LOCALAPPDATA%\Programs\OpenAI\Codex\bin`, e o worker do MCP herda o PATH de quando o app foi aberto. A F5 deve aceitar `CONTRACANTO_CODEX_PATH`, e a pré-checagem da F4 deve recusar cedo quando o executável não resolver (o mesmo tipo de falha do `[WinError 2]`).
5. **O `agy` ignora `--mode auto` nesta conta.** O stderr traz "--mode auto is not available for this account; continuing in the default mode". O Contracanto passa essa flag em toda chamada. Vale registrar o stderr do executor no log (F4) e verificar se o modo padrão tem relação com o caso `executor_noop` do T01.
6. **Preço oficial** (developers.openai.com/api/docs/pricing, por milhão de tokens): input US$ 0,10, cached US$ 0,01, gravação de cache US$ 0,125 (30 min) ou US$ 0,25 (12 h), output US$ 0,50. Acima de 272 mil tokens de entrada: US$ 0,20 / 0,02 / 0,75. Batch e Flex custam a metade. A página não fala da cobrança do Codex via ChatGPT.

## F1: registrar tokens por fase

- `agents.py`:
  - `run_agy` passa a devolver `AgentResult(response, usage)`, com `usage` igual a `None` quando o CLI não informar.
  - `review` extrai `usage` da resposta do `claude`.
  - `call_agent` recebe o nome da fase (`implementation` ou `repair`).
  - Novas tentativas somam os tokens; não os sobrescrevem.
- `state.py`: criar `RunState.add_usage(task_id, phase, usage)`. Ele acumula em `timings[task]['phases'][phase]['tokens']` os campos `input`, `cached_input`, `cache_write`, `output` e `calls`, e mantém um total no nível da execução (`usage`).
- Formato: o campo é opcional, então um `status.json` antigo continua legível.
- Testes: `test_agent_calls.py` (parsing com as fixtures da F0), `test_state_persistence.py` (acúmulo e gravação) e `test_run_report.py`.

**Pronto quando:** uma execução de teste gravar os tokens de cada fase e o total, e uma chamada que não informa tokens não quebrar nada.

**Situação (19/11/2026): implementada, ainda não commitada.** Duas diferenças em relação ao desenho acima:

- **Sem mudar assinaturas:** `run_agy`, `review` e `call_agent` mantêm suas assinaturas, que dezenas de testes simulam. O novo módulo `contracanto/usage.py` normaliza as saídas. `RunState.phase` abre um coletor por thread (`contextvars`), e as chamadas reais lançam nele o que leram (`usage.record`). O total vai para `usage.executor` e `usage.reviewer` no `status.json`.
- **Parser do Codex adiado:** ele fica para a F5, junto com o executor que o usa. A fixture `codex-exec.jsonl` já está pronta.

## F2: custo estimado

- `config.py`: nova variável `CONTRACANTO_PRICES_JSON`, um mapa por modelo com os campos `input`, `cached_input`, `cache_write`, `output` (USD por milhão), mais `long_context_threshold` e as tarifas de contexto longo. Sem a variável, o custo fica ausente e os tokens continuam registrados.
- Valores de referência para o `.mcp.json` (conferir na F0): `gpt-6-luna` com input 0,10, cached 0,01, cache_write 0,125, output 0,50, limite de 272.000 tokens e, acima dele, 0,20 / 0,75.
- `status.json`: `cost_usd` por fase, por tarefa e por execução, mais `billing` (`api` ou `plan`) por papel. O revisor e o arquiteto aparecem como `plan`, com o equivalente apenas informativo.
- `watch.py` e o resumo do `wait_for_run`: uma linha com tokens, custo de API e a parte desperdiçada (tokens e custo de tarefas não aprovadas).

**Pronto quando:** `contracanto --watch` e `wait_for_run` mostrarem o custo de API e o desperdício, e o cálculo tiver teste para cache, contexto longo e preço ausente.

**Situação (19/11/2026): implementada.** Diferenças em relação ao desenho acima:

- **Custo por chamada:** o custo é calculado no momento em que os tokens da chamada são registrados, e não sobre os totais, porque a tarifa de contexto longo vale por pedido. O maior pedido de cada chamada vem dos passos do `agy`.
- **Modelo lido da saída:** o modelo vem da própria saída do CLI. Sem preço configurado, vale o custo que o CLI informa; hoje só o `claude` informa um.
- **Cobrança configurável:** `CONTRACANTO_EXECUTOR_BILLING` e `CONTRACANTO_REVIEWER_BILLING` (padrão `plan`) ficam no lugar da detecção por `codex login status`. Com o login do ChatGPT, o executor Luna fica em `plan`, e passa para `api` se o login for com chave.
- **Sem preço do Gemini:** a página oficial não foi consultada, então as chamadas do `agy` contam em `unpriced_calls` até alguém configurar esse preço.

## F3: categorizar falhas

- `errors.py`: um enum `FailureCategory` com os valores `tooling` (worker, executável ausente, git, preparação), `executor_noop`, `stalled`, `scope`, `patch_too_large`, `inherited_base`, `review_rejected`, `criteria_unverifiable`, `integration` e `cancelled`.
- Exceções próprias, ou um atributo `category`, nos pontos que hoje lançam `RuntimeError`:
  - `run_executor`: `executor_noop`.
  - `review`: `patch_too_large`, e `InheritedFailure` vira `inherited_base`.
  - `gitops.check_scope`: `scope`.
  - `integrate`: `integration`.
  - Erro de processo: `tooling`.
- `criteria_unverifiable`: o revisor ganha o campo opcional `unverifiable: true` no JSON do veredito quando a rejeição é só porque o comando de teste não comprova o critério.
- `runner.py`: `collect`, `review_task` e `integrate_task` gravam `details[task]['failure'] = {category, phase, message}`.
- Resumo: `wait_for_run` passa a trazer `failures_by_category` e `wasted_seconds`.

**Pronto quando:** cada caso da tabela inicial, reproduzido em teste, cair na categoria certa.

**Situação (19/11/2026): implementada.** Diferenças em relação ao desenho acima:

- **Marca em vez de classes novas:** a categoria é gravada na própria exceção (`errors.tag`), sem trocar tipo nem mensagem. Exceções sem marca são classificadas pelo tipo (`errors.failure_category`).
- **Revisão:** `agents.review` devolve um `ReviewResult`, que continua desempacotável como o par `(aprovado, parecer)` e traz `unverifiable_only`.
- **Categorias a mais:** `timeout`, `reviewer_error` e `other`. A integração foi separada em `integration_conflict` e `integration_tests`.
- **Onde aparecem:** o desperdício fica em `waste`, no `status.json` e no resumo do `wait_for_run`, e o `--watch` mostra a categoria antes de cada erro.
- **Fica para a F4:** `stalled`, que depende do watchdog.

## F4: falhar cedo e detectar executor parado

- **Pré-checagem** em `submit_plan` e `resume_run`, antes de reservar a execução:
  - O executor configurado existe no PATH, assim como `claude` e `git`.
  - O modelo aparece na lista do executor (`agy models`, ou o equivalente do Codex), com cache curto.
  - O primeiro executável dos comandos de teste e de preparação resolve.
  - Falhas aqui viram erro imediato, sem ocupar o worker. Isso cobre o caso `[WinError 2]`.
- **Watchdog de inatividade:**
  - `process.run` ganha `idle_check: Callable[[], bool]` e `idle_timeout`.
  - Para o executor, a checagem compara a cada 30 s um hash de `git status --porcelain` com os mtimes dos arquivos alterados, e também considera saída nova no stdout.
  - Sem mudança dentro de `CONTRACANTO_AGENT_IDLE_TIMEOUT` (padrão 600 s; 0 desliga), o processo é encerrado com a categoria `stalled`. Isso conta como uma tentativa normal de `AGENT_RETRIES`.
- **Worker morto em 1–2 s:** gravar o stderr do worker em `run_dir/worker.err` e anexar a cauda ao `error`, para que "encerrado sem registrar o estado final" venha com a causa.

**Pronto quando:** um executor falso que dorme sem escrever for encerrado após o `idle_timeout`, um executável ausente for recusado no `submit_plan`, e um worker que morre deixar a causa no `status.json`.

**Situação (19/11/2026): implementada.** Diferenças em relação ao desenho acima:

- **Logs do worker:** o stderr do worker já ia para o `run.log`. As execuções que morreram em 1–2 s não têm traceback: foram encerradas de fora, no meio da implementação, antes de o worker passar a rodar fora do Job Object do app. Por isso, em vez de um `worker.err`, a liberação da reserva grava o último evento do log no `error`.
- **`[WinError 2]` do c1b9:** já tinha sido corrigido pelo `ec50a13`, porque a base rodava o comando de testes sem resolver o `.cmd` do npm.
- **Pré-checagem:** é uma função só (`runner.preflight`), usada pelo worker e pelo `_launch` do MCP, que cobre submissão, retomada e retomada automática. O modelo é conferido em `agy models`, com cache de 10 min; se a lista falhar, a execução não é bloqueada.
- **Watchdog:** com ele ligado, o `process.run` grava a saída em um arquivo temporário e mede o crescimento dela, junto com o estado do worktree (arquivos alterados, tamanho e horário).
- **Stderr do `agy`:** vai para o `run.log`, o que mostra avisos como o do `--mode auto`.

## F5: executor plugável e GPT-6 Luna

- `config.py`: `CONTRACANTO_EXECUTOR` aceita `agy` (padrão, o comportamento atual) ou `codex`. `CONTRACANTO_AGENT_MODEL` continua valendo para os dois.
- `agents.py`:
  - Criar o protocolo `Executor.run(prompt, wt, stop) -> AgentResult`.
  - O `run_agy` atual vira `AgyExecutor`, sem mudar comportamento (commit estrutural).
  - Criar `CodexExecutor` (commit comportamental):
    - Comando, validado na F0: `codex exec --json --model <modelo> --sandbox workspace-write --ephemeral --ignore-user-config -c approval_policy="never" -c windows.sandbox="elevated" -C <wt> -`, com o prompt pela entrada padrão, como já é feito com o `agy`, por causa do limite de linha de comando do Windows. O executável vem de `CONTRACANTO_CODEX_PATH` ou do PATH.
    - A resposta vem da última mensagem do agente, e `usage` da soma dos eventos de fim de turno.
    - Status diferente de sucesso vira `RuntimeError`, que o retry já trata.
- Prompts:
  - `IMPLEMENT_PROMPT` deixa de citar só o `GEMINI.md`: "Leia AGENTS.md e, se existir, o arquivo de instruções do seu CLI". O Sanagua já tem `AGENTS.md`.
  - Revisar se `REPAIR_PROMPT` assume algo específico do `agy`.
- Segurança: rede desligada no sandbox, a mesma regra de escopo de arquivos (`check_scope` continua sendo a barreira real) e nenhum segredo no `.mcp.json`. A chave da OpenAI fica na variável de ambiente do usuário e nunca é gravada em repositório nem em log.
- Contexto longo: configurar o Codex para compactar o histórico antes de 272.000 tokens, para não cair na tarifa dobrada. A F2 sinaliza quando uma chamada ultrapassar o limite.
- Testes: um executor falso com saída do Codex (fixture da F0), retry, sandbox e registro de `usage`.

**Pronto quando:** `CONTRACANTO_EXECUTOR=codex` rodar uma tarefa de exemplo (`examples/smoke_check.py`) até `approved`, com tokens e custo no `status.json`.

**Situação (19/11/2026): implementada.** Diferenças em relação ao desenho acima:

- **Sem protocolo `Executor`:** `agents.run_agent` escolhe entre `run_agy` e `run_codex` pela configuração. Assim, os testes que simulam `run_agy` continuam valendo.
- **Tokens do Codex:** `usage.from_codex` soma os turnos. O modelo vem de `CONTRACANTO_AGENT_MODEL`, e não há pico por pedido, então o contexto longo não é detectado.
- **Compactação antes de 272 mil tokens:** não foi configurada. A chave do Codex não foi verificada, e `--ignore-user-config` ignoraria uma configuração do usuário.
- **Pré-checagem:** procura `CONTRACANTO_CODEX_PATH` e não consulta `agy models` com o Codex.
- **Smoke real:** `approved`, com tokens no `status.json`. Na primeira tentativa, os comandos de shell do Codex falharam com `CreateProcessWithLogonW failed: 267`, porque o app Claude (MSIX) redireciona `%LOCALAPPDATA%`, onde ficam os worktrees por padrão, para a pasta do pacote, que o usuário do sandbox não alcança. Com `CONTRACANTO_WORKTREES_DIR` fora de `AppData\Local`, os comandos rodaram. O custo do executor ficou sem preço porque `CONTRACANTO_PRICES_JSON` não estava definido.

## F6: regras para o arquiteto dimensionar tarefas

Itens a incluir no `CLAUDE.md` do Contracanto:

- Toda tarefa precisa caber no limite de revisão. Formatação em massa, como Prettier no repositório inteiro, vira um commit do operador e não uma tarefa (o caso do T02).
- Tarefas estruturais grandes devem ser divididas por módulo, e não pedir `src/` e `test/` inteiros (o caso do T05, resolvido ao dividir em T05a e T05b).
- Critérios de aceitação só podem citar o que o comando de teste fixado comprova. "Estável em duas execuções" ou checagens manuais não entram.
- Uma tarefa que já falhou com a categoria `review_rejected` duas vezes deve ser replanejada, não reenviada com `resume_run`.

Aviso não bloqueante no `submit_plan`:

- `files` com dois ou mais diretórios raiz inteiros.
- Critério que cita um comando diferente do `test_command`.
- Mais de N arquivos previstos.

O aviso vai na resposta da ferramenta, para o arquiteto decidir.

**Pronto quando:** o `CLAUDE.md` tiver as regras e os avisos tiverem teste.

## F7: piloto comparativo

- Escolher, com o responsável, a próxima leva real de tarefas do Sanagua. As fases 4 e 8 do plano de refatoração dependem de decisões dele.
- Rodar 2–3 tarefas com `codex` + `gpt-6-luna` e tarefas equivalentes com `agy` + `gemini-4-hydrogen-high`. Comparar:
  - aprovação na primeira revisão;
  - número de reparos;
  - tempo total;
  - tokens e custo de API;
  - desperdício por categoria.
- Metas iniciais:
  - desperdício abaixo de 25% do tempo;
  - custo de API abaixo de US$ 0,50 por tarefa aprovada;
  - nenhuma falha da categoria `tooling`.
- Registrar o resultado e decidir o executor padrão.

## Estimativa de esforço

| Fase | Tamanho | Observação |
|---|---|---|
| F0 | 1–2 h | Exige instalar o Codex CLI e uma chave da OpenAI, que o responsável configura |
| F1 | médio | Mexe em `agents.py`, `state.py` e testes |
| F2 | pequeno | Cálculo puro e exibição |
| F3 | médio | Muitos pontos de lançamento de erro |
| F4 | médio | O watchdog no `process.run` exige cuidado com Windows e o job object |
| F5 | médio–grande | Novo executor, prompts e segurança do sandbox |
| F6 | pequeno | Texto e avisos |
| F7 | 1 execução real | Custo esperado de alguns dólares de API |
