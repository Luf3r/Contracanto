# Contracanto

Claude planeja e revisa. Antigravity (ou, se configurado, o Codex CLI) implementa cada tarefa em um worktree Git. Um servidor MCP em Python coordena os dois e integra as mudanças após a revisão e os testes.

Ferramenta de integração local, exercitada com os agentes reais no Windows (execução, branch de integração, retomada manual e automática); a suíte automatizada roda em Windows e Linux. Cada máquina precisa validar suas credenciais e permissões. A revisão é feita por um modelo e não substitui a sua: em um repositório real, integre em uma branch própria com `--branch` e revise antes de juntar ou publicar.

## Pré-requisitos
- Windows, Python 3.13–3.15 e Git for Windows com suporte a `git merge-tree --write-tree` para integrar tarefas paralelas. Ambiente local validado: Python 3.15.2, MCP 1.30.0, `agy` 1.5.8 e Claude Code 2.1.325. A matriz de CI está configurada para Python 3.13 e 3.15 em Windows e Linux; confira o resultado do workflow antes de adotar outra combinação.
- Aplicativo Claude Desktop (aba Code, sessão **Local**)
- Claude Code CLI instalado e autenticado (`claude --version`, `claude -p "oi"`)
- Antigravity App e CLI (`agy --version`), com autenticação realizada interativamente
- O repositório deve estar limpo; faça a primeira execução em um projeto de teste

Instale o CLI do Antigravity seguindo a documentação oficial https://antigravity.google/docs/getting-started . A instalação Windows documentada é:

```powershell
irm https://antigravity.google/cli/install.ps1 | iex
agy --version
agy
```

## Preparação
1. Crie um venv para o Contracanto, fora do repositório que ele vai modificar, e instale o pacote a partir desta cópia do código (`CAMINHO` é o diretório dela):

```powershell
py -m venv C:\ferramentas\contracanto-venv
C:\ferramentas\contracanto-venv\Scripts\Activate.ps1
python -m pip install -r CAMINHO\requirements.lock.txt
python -m pip install CAMINHO
python -m pip check
contracanto --version
claude --version
agy --version
```

A instalação cria os comandos `contracanto` (CLI) e `contracanto-mcp` (servidor MCP).

2. Escolha o repositório a orquestrar com `--repo CAMINHO_DO_PROJETO` na CLI ou com a variável `CONTRACANTO_REPO` no ambiente do servidor MCP; sem eles, vale o diretório atual. O caminho precisa ser **a raiz** de um repositório Git, com uma branch em uso (HEAD destacado é recusado, salvo com `--branch`) e sem alterações pendentes. Um subdiretório do repositório é recusado.
3. O estado das execuções fica em `.agent-runs/` dentro desse repositório, que precisa ignorar o diretório para continuar limpo: acrescente `.agent-runs/` ao `.gitignore` ou a `.git/info/exclude`. Os worktrees das tarefas ficam fora do repositório, em `~/.cache/Contracanto/worktrees/<repositório>-<hash>/` (ou sob `$XDG_CACHE_HOME`), também no Windows, para que test runners, linters e `tsc` com globs amplos não enxerguem as cópias. `CONTRACANTO_WORKTREES_DIR` escolhe outra pasta. Se a saída do teste de integração citar caminhos em `.agent-runs/`, por exemplo worktrees de execuções anteriores a essa mudança, o erro da tarefa e o campo `warning` do status avisam; exclua o diretório dos globs e limpe os registros antigos com `--prune-runs`.
4. Com o Antigravity, o executor e todas as correções passam explicitamente `--mode auto --sandbox`, sem `--dangerously-skip-permissions`. Configure uma allowlist limitada no CLI antes de operar. Comandos negados devem ser autorizados de forma granular; não desative o sandbox para contornar a falha. O modo headless pode negar comandos não autorizados. Configure permissões granulares conforme docs: https://antigravity.google/docs/cli/headless/

Sem instalar, `python -m contracanto` e `python -m contracanto.server` equivalem a `contracanto` e `contracanto-mcp`, desde que o diretório desta cópia esteja no `PYTHONPATH`. Para desenvolver o próprio Contracanto, instale-o no venv dele com `python -m pip install -e ".[dev]"`.

## Executor Codex

Com `CONTRACANTO_EXECUTOR=codex`, o Codex CLI implementa e corrige no lugar do `agy`. A revisão continua com o `claude`. Cada chamada roda assim:

```text
codex exec --json --sandbox workspace-write --ephemeral --ignore-user-config
  -c approval_policy="never" [-c windows.sandbox="elevated"] [--model <modelo>] -C <worktree> -
```

O pedido vai pela entrada padrão.

- **Configuração do usuário fica de fora.** `--ignore-user-config` deixa de fora plugins, skills e hooks do `~/.codex`, que numa medição quase dobraram o contexto da primeira chamada (de 64 mil para 118 mil tokens). O login continua valendo. A política de aprovação e, no Windows, o sandbox `elevated` vêm explícitos: sem o primeiro, todo comando do agente é recusado; sem o segundo, o worktree fica só para leitura.
- **Sandbox `workspace-write`:** a rede fica bloqueada, e a escrita só é permitida no worktree e na pasta temporária (`%TEMP%`) do usuário.
- **Worktrees fora de `%LOCALAPPDATA%`:** o app Claude é um pacote MSIX, e o que os processos abertos por ele gravam em `%LOCALAPPDATA%` vai para a pasta do pacote (`AppData\Local\Packages\Claude_…\LocalCache`). O usuário do sandbox do Codex não chega lá, e todo comando de shell falharia com `CreateProcessWithLogonW failed: 267`. Por isso o padrão dos worktrees é `~/.cache/Contracanto/worktrees`; se definir `CONTRACANTO_WORKTREES_DIR`, mantenha a pasta fora de `AppData\Local`.
- **Instruções do projeto:** o Codex lê o `AGENTS.md` do worktree, e o pedido de implementação manda ler `AGENTS.md` e `GEMINI.md` quando existirem.
- **Tokens e modelo:** os tokens vêm dos eventos `turn.completed`. O Codex não informa o modelo nem cada pedido ao modelo, então o modelo registrado é o de `CONTRACANTO_AGENT_MODEL`, e uma chamada acima do limite de contexto longo não é detectada.
- **Cobrança:** com o login do ChatGPT, o uso sai do limite do plano. Deixe `CONTRACANTO_EXECUTOR_BILLING` em `plan` e use `api` só com login por chave de API.

Exemplo de `env` no `.mcp.json`, com o caminho do executável no lugar de `<caminho>`:

```json
"CONTRACANTO_EXECUTOR": "codex",
"CONTRACANTO_CODEX_PATH": "<caminho>/codex.exe",
"CONTRACANTO_AGENT_MODEL": "gpt-6-luna"
```

## Teste sem MCP
Copie `examples/plan.example.json`, edite com tasks reais e arquivos autorizados e faça o primeiro teste em um repositório descartável. Exemplo, a partir da raiz do repositório:

```powershell
contracanto plano.json --parallel 3 --retries 2 --test py -m pytest
```

O plano de exemplo é ilustrativo; adaptações são necessárias conforme o stack. Sem pytest instalado, configure um comando válido antes de executar. Para um primeiro teste, use --parallel 1.

`--test` deve ser a **última opção**: todos os argumentos seguintes pertencem ao comando de testes, inclusive `-m`, `-v` e outras flags. `contracanto --help` lista todas as opções e `--version` mostra a versão. `--repo CAMINHO` escolhe o repositório a orquestrar e `--keep-worktrees` preserva worktree e branch das tarefas aprovadas.

Por padrão, cada tarefa aprovada vira um commit na branch em uso, inclusive a `main`. Com `--branch NOME`, o worker cria essa branch no commit atual, passa o checkout para ela e integra ali; a branch de origem não recebe nada e você decide depois se e como juntar. O nome precisa ser novo. O checkout fica na branch nova ao final. Se nenhuma tarefa for integrada, o worker volta o checkout para onde estava e apaga a branch vazia (`status.target.branch_removed`), salvo quando a execução exige recuperação manual. `status.target` registra a branch de destino e o commit de partida.

Cada tarefa roda em um worktree novo, que só contém os arquivos rastreados pelo Git: `.venv`, `node_modules` e outros diretórios ignorados não existem ali. Se os testes dependem deles, informe um comando de preparação com `--setup-json`, uma lista JSON de argumentos, antes de `--test`:

```powershell
contracanto plano.json --setup-json '[\"npm\",\"ci\"]' --test npm test
```

O comando roda em cada worktree antes da implementação, com as mesmas permissões do comando de testes, e só pode criar arquivos ignorados pelo Git; se falhar ou deixar outros arquivos, a tarefa falha. O PowerShell 5.1 exige as aspas internas escapadas com `\"`, como no exemplo; no PowerShell 7 e no bash basta `'["npm","ci"]'`.

Erros de uso e de preparação, como plano inexistente, repositório com alterações ou valor inválido em uma variável `CONTRACANTO_*`, saem em uma linha iniciada por `Erro:`, com código de saída diferente de zero. As opções de execução (`--test`, `--setup-json`, `--branch`, `--keep-worktrees`) são recusadas junto de `--status`, `--watch`, `--stop`, `--unlock` e `--prune-runs`. Pelo MCP, `submit_plan` e `resume_run` conferem o repositório antes de reservar a execução: alterações pendentes, HEAD destacado sem `branch` e nome de branch inválido ou já usado voltam como `Erro:` na própria chamada. Antes de reservar a execução, `submit_plan` e `resume_run` também conferem o ambiente, e o worker repete a conferência antes de chamar qualquer agente: o executável do executor (`agy`, ou `CONTRACANTO_CODEX_PATH` com o Codex) e o `claude` no PATH, o executável dos comandos de testes e de preparação e, com o `agy` e `CONTRACANTO_AGENT_MODEL` definido, se o modelo aparece em `agy models`. Se essa lista não puder ser obtida, a execução segue. No Windows, um script `.cmd` ou `.bat` encontrado no PATH, como `npm`, é chamado pelo caminho completo.

## Operação pela linha de comando

| Comando | Efeito |
| --- | --- |
| `contracanto --status` | Mostra, em JSON, o estado da execução ativa ou da mais recente (o mesmo de `get_run_status`) |
| `contracanto --watch` | Painel no terminal que acompanha a execução até ela terminar: tarefas, fase atual, tempos, erros e últimos eventos. Fora de um terminal, escreve uma linha por mudança e, no fim, o resumo de `wait_for_run`. Sai com 0 se todas as tarefas foram aprovadas, 1 se não, 3 se o worker morreu |
| `contracanto --stop` | Pede o encerramento cooperativo da execução ativa |
| `contracanto --resume [RUN_ID] --test ...` | Executa de novo só as tarefas não aprovadas de uma execução encerrada (padrão: a mais recente) |
| `contracanto --unlock` | Remove a reserva de um worker que já terminou, depois de conferir o processo e o Git |
| `contracanto --prune-runs N` | Remove os registros das execuções encerradas, mantendo as N mais recentes |
| `contracanto --prune-runs N --force` | O mesmo, descartando também worktrees com alterações não salvas |

Todos aceitam `--repo CAMINHO`. `--unlock` recusa, com código de saída 1, enquanto o worker da execução ou um comando iniciado por ele estiver vivo, houver operação Git pendente ou o repositório tiver alterações. `--prune-runs` nunca remove a execução reservada, uma sem estado final ou uma com recuperação pendente. Sem `--force`, também mantém a execução inteira quando algum worktree dela tem alterações, o que é comum em tarefas que falharam; com `--force`, essas alterações e as branches `agent/*` são descartadas.

`--resume` (ou a ferramenta MCP `resume_run`) cria uma execução nova com o plano da anterior, sem as tarefas aprovadas e sem as dependências que apontavam para elas. Aceita as mesmas opções de uma execução comum. É recusada se o commit integrado de alguma tarefa aprovada não estiver mais no histórico do HEAD atual, por exemplo depois de trocar de branch ou desfazer commits. Também é recusada quando o commit de uma tarefa não aprovada continua no HEAD sem ter sido revertido, o que acontece se o worker for encerrado à força durante a integração: desfaça o commit com `git revert` ou submeta um plano novo sem a tarefa. Se você já o desfez de outra forma, que a retomada não reconhece, `contracanto --resume --force` dispensa essa conferência; a opção existe só na linha de comando.

Os ajustes abaixo são feitos por variáveis de ambiente. No MCP, defina-as em `"env"` na configuração do servidor; o worker herda o ambiente. Um valor inválido interrompe a inicialização com o nome da variável.

| Variável | Padrão | Efeito |
| --- | --- | --- |
| `CONTRACANTO_TEST_TIMEOUT` | 600 | Segundos para cada execução do comando de testes |
| `CONTRACANTO_SETUP_TIMEOUT` | 600 | Segundos para o comando de preparação de cada worktree |
| `CONTRACANTO_AGENT_TIMEOUT` | 1600 | Segundos para cada chamada ao `agy`; mínimo 160 |
| `CONTRACANTO_AGENT_IDLE_TIMEOUT` | 600 | Segundos sem saída nova do `agy` nem alteração de arquivos no worktree até a chamada ser encerrada como parada (categoria `stalled`), contando como uma tentativa; `0` desliga |
| `CONTRACANTO_REVIEW_TIMEOUT` | 900 | Segundos para cada chamada ao revisor |
| `CONTRACANTO_GIT_TIMEOUT` | 180 | Segundos para cada comando Git |
| `CONTRACANTO_REVIEW_MODEL` | `sonnet` | Modelo usado pelo revisor |
| `CONTRACANTO_EXECUTOR` | `agy` | CLI que implementa e corrige: `agy` (Antigravity) ou `codex` (Codex CLI); veja [Executor Codex](#executor-codex) |
| `CONTRACANTO_CODEX_PATH` | `codex` | Executável do Codex; prefira o caminho completo, porque o servidor MCP herda o PATH de quando o app foi aberto |
| `CONTRACANTO_AGENT_MODEL` | sem valor | Modelo do executor, passado em `--model` (no `agy`, ids em `agy models`, por exemplo `gemini-4-hydrogen-high`; no Codex, por exemplo `gpt-6-luna`); sem valor, vale o padrão do CLI |
| `CONTRACANTO_AGENT_RETRIES` | 1 | Novas chamadas ao `agy` quando uma implementação ou correção falha (0 a 3) |
| `CONTRACANTO_AUTO_RESUME` | 1 | Vezes que `wait_for_run` relança sozinho uma execução cujo worker morreu (0 a 3; 0 desliga) |
| `CONTRACANTO_WORKTREES_DIR` | `~/.cache/Contracanto/worktrees` | Pasta onde ficam os worktrees das tarefas, separada por repositório; mantenha-a fora do repositório orquestrado |
| `CONTRACANTO_REVIEW_PATCH_LIMIT` | 400000 | Tamanho máximo do patch enviado ao revisor, em caracteres |
| `CONTRACANTO_PRICES_JSON` | sem valor | Preço por modelo, em dólares por milhão de tokens, para estimar o custo de cada chamada; formato em [docs/recuperacao.md](docs/recuperacao.md#custo). Sem valor, só o custo que o próprio CLI informa é usado |
| `CONTRACANTO_EXECUTOR_BILLING` | `plan` | `api` quando o executor é cobrado por token; `plan` quando sai do limite de uma assinatura, e o custo vale só como equivalente |
| `CONTRACANTO_REVIEWER_BILLING` | `plan` | O mesmo para o revisor (`claude -p`) |
| `CONTRACANTO_TEST_COMMAND` | sem valor | Lista JSON de argumentos. Fixa o comando de testes: um comando diferente é recusado |
| `CONTRACANTO_SETUP_COMMAND` | sem valor | Lista JSON de argumentos. Fixa o comando de preparação: um diferente é recusado e, sem nenhum informado, ele é usado; `[]` proíbe qualquer preparação |

Sem as duas últimas variáveis, quem chama `submit_plan` escolhe os comandos, e no uso pelo MCP quem chama é o modelo. Para que só o operador decida o que roda com as permissões dele, defina as duas em `"env"` do servidor, por exemplo `"CONTRACANTO_TEST_COMMAND": "[\"py\",\"-m\",\"pytest\"]"` e `"CONTRACANTO_SETUP_COMMAND": "[]"`. A comparação é exata, argumento por argumento, e a recusa informa o comando esperado. A conferência vale também para a CLI executada com essas variáveis no ambiente.

## Testes deste projeto

Os testes usam `unittest`, incluído no Python, e Git local. Não chamam os CLIs dos modelos nem consomem tokens. O executor paralelo roda cada teste em um processo próprio, com um processo por núcleo por padrão:

```powershell
.\.venv\Scripts\python.exe scripts/run_tests.py
```

`-j N` limita os processos, `-v` mostra cada teste com o tempo gasto e um texto qualquer seleciona os testes cujo identificador o contém (por exemplo `scripts/run_tests.py test_scope_gates`). Um teste que passa de `--timeout` segundos (padrão 300) é encerrado com seus subprocessos e aparece como falha, com o nome dele. `python -m unittest discover -s tests -v` continua funcionando, de forma serial.

Há regressões de parsing da CLI, IDs que diferem apenas por maiúsculas/minúsculas, escopo com Unicode/espaços/renomeações, prompts grandes por stdin, estado terminal, reserva de execução e rollback. Há testes com Git e subprocessos reais para encerramento de descendentes por timeout, execução paralela, dependências e rejeição seguida de correção e integração. Os modelos são simulados; os testes de recuperação também injetam falhas operacionais, incluindo erros de leitura do Git após os testes de integração. A CI executa a suíte e `pip check`. No Windows, os testes cobrem também encerramento após a saída do processo pai, indisponibilidade de `taskkill`, falha de associação ao Job Object e isolamento entre comandos paralelos.

Para testar os CLIs reais, use uma cópia descartável deste projeto com Git limpo, autenticações ativas e permissões adequadas. O smoke cria apenas `smoke.txt` e **consome chamadas dos modelos**:

```powershell
.\.venv\Scripts\python.exe -m contracanto examples/plan.smoke.json --parallel 1 --retries 0 --test python examples/smoke_check.py
```

O processo deve retornar código 0 e a task `smoke` deve terminar `approved`. O arquivo deve conter `orchestrator-ok` e uma quebra de linha. O comando `python` precisa apontar para um interpretador real no PATH dos workers; se necessário, substitua-o pelo caminho absoluto do Python do venv.

## Formato do plano

O plano é um objeto JSON com `tasks` (1 a 100 tarefas). `goal` é contexto descritivo opcional, enviado ao executor, ao revisor e em cada tentativa de correção. Cada tarefa exige:

| Campo | Contrato |
| --- | --- |
| `id` | String única sem distinguir maiúsculas/minúsculas, 1 a 60 caracteres: letras ASCII, números, `_` ou `-`. IDs como `A` e `a` não podem coexistir no mesmo plano, em nenhuma plataforma |
| `description` | String não vazia com a instrução de implementação |
| `deps` | Lista de IDs existentes, com a grafia exata declarada em `id`; pode ser vazia; sem autorreferência ou ciclos |
| `files` | Lista não vazia de arquivos ou diretórios relativos, usando `/` |
| `acceptance` | Lista não vazia de strings com critérios verificáveis |

Diretórios autorizam seus descendentes. Caminhos absolutos, `..`, `.`, `\\`, componentes vazios, `.git` e `.agent-runs` são rejeitados. A comparação de ownership é textual; use a grafia exata e consistente dos caminhos, inclusive maiúsculas/minúsculas. A verificação de escopo é posterior à execução e não é uma sandbox.

O MCP exige `plan_json` e `test_command_json`. Ambos são strings contendo JSON. Exemplo de argumentos de `submit_plan`:

```json
{
  "plan_json": "{\"tasks\":[{\"id\":\"smoke\",\"description\":\"Crie smoke.txt com orchestrator-ok e uma quebra de linha\",\"deps\":[],\"files\":[\"smoke.txt\"],\"acceptance\":[\"Conteúdo exato\"]}]}",
  "parallel": 1,
  "retries": 0,
  "test_command_json": "[\"python\",\"examples/smoke_check.py\"]"
}
```

`setup_command_json` é opcional e tem o mesmo formato de `test_command_json`: é o comando de preparação de cada worktree, equivalente a `--setup-json`. `branch`, também opcional, equivale a `--branch`.

Quem fornece `--test` ou `test_command_json` escolhe um comando local. O worker recebe uma lista não vazia de argumentos e inicia o executável diretamente. O comando e o código que ele carrega rodam fora do sandbox do Antigravity, com as permissões do usuário, inclusive acesso à rede, às credenciais e a arquivos fora do repositório. Podem fazer push ou deploy. Use apenas comandos, planos e código de origem confiável. A primeira execução ocorre no worktree antes do parecer do revisor. Cada nova revisão repete o teste, e a integração o executa novamente na branch de destino.

`parallel` aceita 1 a 8. `retries` aceita 0 a 3 e controla as tentativas de correção após rejeição da revisão. Uma falha da própria chamada ao `agy`, como um timeout ou um erro do CLI, é outra coisa: na implementação ou em uma correção, a chamada é repetida no mesmo worktree até `CONTRACANTO_AGENT_RETRIES` vezes, desde que a branch e o commit do worktree estejam intactos e o encerramento dos processos tenha sido confirmado.

Se os testes do worktree falharem, o executor recebe o parecer do revisor, o código de saída e o trecho final da saída dos testes na tentativa de correção. Se as tentativas se esgotarem, consulte o diagnóstico em `details.<id>.review` e o resumo em `details.<id>.error` no status da tarefa.

O comando de testes deve preservar o commit atual, a branch ativa e deixar o Git limpo. Trocar de branch mesmo mantendo o mesmo commit, alterar arquivos rastreados ou criar arquivos não ignorados impede a aprovação, mesmo com código de saída 0. Artefatos ignorados pelo Git, como caches, são permitidos. No worktree, a tarefa falha e as alterações ficam disponíveis para inspeção. Na branch de destino, mudanças de branch, alterações ou commits feitos pelos testes exigem recuperação manual: a reserva é mantida e nenhuma outra tarefa integra, inclusive se o comando falhar ou sofrer timeout. Não há limpeza destrutiva automática desses arquivos.

Merge, cherry-pick, revert, rebase, `git am` e bisect em andamento também impedem a aprovação, inclusive quando `git status --porcelain` está vazio. Se os testes de integração deixarem uma dessas operações aberta na branch de destino, a reserva permanece ativa para recuperação manual.

Para arquivos rastreados com `assume-unchanged` ou `skip-worktree`, o worker compara o conteúdo em disco com o HEAD usando um índice Git temporário. A verificação preserva as marcas do índice local e alcança os submódulos inicializados. Em um checkout parcial, o worker usa `git sparse-checkout check-rules` para reconhecer arquivos ausentes conforme as regras de seleção.

A verificação de limpeza inclui submódulos inicializados mesmo quando configurados com `ignore = all`. Antes do cherry-pick, o worker verifica se a integração sobrescreveria dados locais não rastreados, inclusive arquivos ignorados. Uma colisão faz a tarefa falhar antes de alterar a branch. Se a colisão surgir no caminho de um revert após os testes, os dados ficam preservados e a reserva permanece ativa para recuperação manual.

## Conectar ao Claude Desktop na aba Code
Crie o `.mcp.json` **na raiz do repositório a orquestrar**, com o caminho absoluto do comando instalado e do próprio repositório. O arquivo é local e não deve ser versionado; `.mcp.example.json` serve de modelo:

```json
{
  "mcpServers": {
    "contracanto": {
      "command": "C:\\CAMINHO\\DO\\VENV\\Scripts\\contracanto-mcp.exe",
      "env": {"CONTRACANTO_REPO": "C:\\CAMINHO\\DO\\PROJETO"}
    }
  }
}
```

O nome do servidor (`contracanto` no exemplo) é o prefixo das ferramentas nas permissões do Claude Code; ao renomear um servidor já configurado, as permissões concedidas ao nome antigo não valem para o novo.

Alternativamente, configure o MCP no ambiente local do Claude Code. O MCP de Chat e o da aba Code não são os mesmos. Reinicie a sessão local do Claude e verifique se aparecem as ferramentas `submit_plan`, `resume_run`, `wait_for_run`, `get_run_status`, `stop_run` e `get_submitted_plan`.

Peça ao Claude na aba Code:

> Inspecione este repositório, proponha um plano em tarefas pequenas e verificáveis com dependências e arquivos autorizados; use submit_plan para iniciar execução com 3 workers e testes `py -m pytest`. Revise as tasks automaticamente via Claude CLI. Não faça deploy/push ou modificações destrutivas. Rode em segundo plano o comando `--watch` que a resposta traz e consulte get_run_status quando ele terminar.

## Comportamento e limitações
- O Claude cria o grafo de tarefas e envia pelo MCP. A fila é executada por Python, chamando `agy` em worktrees locais isolados.
- O comando de preparação (`setup_command_json`, `--setup-json`) roda em cada worktree antes da implementação e também na raiz, antes dos testes de integração: na primeira integração da execução e, depois, sempre que um arquivo de dependências mudou desde a última preparação (`package.json`, `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `requirements*.txt`, `pyproject.toml`, `poetry.lock`, `go.mod`, `Cargo.toml` e outros, em qualquer diretório), inclusive quando uma integração revertida desfez a mudança. Na raiz, como nos worktrees, ele só pode criar arquivos ignorados pelo Git. Se falhar, a integração é revertida e o erro traz a saída da preparação.
- O erro de uma integração que falhou traz o código de saída e a cauda da saída dos testes, onde a causa costuma estar. Os resumos de `wait_for_run`, do `--watch` e do painel mantêm a primeira linha do erro e a cauda, com `[…]` no corte.
- O comando de testes é a única verificação que roda: na base, no worktree e na integração. Use a verificação completa do projeto (testes, lint, tipos e build, por exemplo `npm run check`), não só a suíte de testes. O revisor recebe o comando em `test_command` e rejeita um critério que dependa de outra verificação, dizendo qual faltou.
- Antes da primeira implementação de cada lote, o worker roda o comando de testes na base, uma vez, no primeiro worktree preparado. Se a base já falha, o executor recebe o resultado em `base_tests` e o revisor em `base_test_exit_code` e `base_test_output`. A aprovação continua exigindo testes verdes: uma tarefa que conserta a base segue normalmente, e uma que não conserta falha logo depois da implementação, sem revisão nem correções, com as saídas da base e do worktree no erro. Planeje primeiro uma tarefa que corrija a base. O revisor separa defeito herdado de defeito da tarefa: o que já existia na base vai em `inherited` e não impede a aprovação, salvo quando a tarefa pede a correção.
- O Claude Code CLI revisa o patch usando o alias `sonnet` (ou `CONTRACANTO_REVIEW_MODEL`), sem ferramentas internas e sem carregar servidores MCP. Ele roda em um diretório temporário vazio, fora do worktree, para não carregar o `CLAUDE.md` nem outras instruções do projeto revisado; tudo o que ele avalia chega pelo pedido. O revisor responde com um objeto JSON (`verdict`, `summary`, `problems`); o orquestrador aceita esse objeto mesmo cercado de texto ou de uma cerca de código. Qualquer outra resposta, inclusive `APPROVED` fora de um objeto JSON, conta como rejeição, e a aprovação exige também que os testes do worktree passem. O revisor recebe os diffs de texto e os patches binários no formato aplicável do Git. Para um critério que depende do conteúdo de uma imagem ou de outro binário, inclua um teste específico desse conteúdo. Prompts seguem por stdin para evitar o limite de argumentos do Windows. Um patch maior que `CONTRACANTO_REVIEW_PATCH_LIMIT` faz a tarefa falhar com essa indicação, em vez de ser revisado pela metade; divida a tarefa.
- Tarefas independentes, que não anunciam ownership conflitante, podem rodar em paralelo. Implementação, revisão e correções de um lote rodam em paralelo, cada tarefa no próprio worktree; a integração na branch de destino é sequencial, na ordem em que as implementações terminaram. Se uma revisão exigir recuperação manual, nenhuma tarefa do lote é integrada. Quando uma tarefa conflita com outra já integrada no mesmo lote, ela falha e `details.<id>.error` lista os arquivos em conflito.
- No pedido ao revisor, `patch_encoding` identifica a codificação usada para representar os bytes do patch. Codificar o campo `patch` com esse valor reproduz a saída original do Git, inclusive quebras de linha e acentos de arquivos legados.
- Após cada implementação ou correção, o orquestrador confere a branch atribuída ao worktree e o commit esperado antes de registrar as mudanças. Uma troca de branch no mesmo commit também faz a tarefa falhar. O escopo é conferido pelo diff completo entre a base e o candidato antes de cada revisão, incluindo origem e destino de renomeações.
- A integração termina na branch local. Depois de integrada e aprovada, a tarefa tem seu worktree e sua branch `agent/*` removidos; `details.<id>.worktree_removed` registra a limpeza e `cleanup_error` uma eventual falha, que não altera o resultado. Use `--keep-worktrees` (ou `keep_worktrees` em `submit_plan`) para preservá-los. Tarefas que falham, inclusive por conflito de cherry-pick, sempre mantêm worktree e branch para inspeção.
- Antes do cherry-pick, o worker compara os caminhos da integração prevista com a lista `files` da tarefa. Se uma tarefa anterior moveu um diretório e o Git redirecionaria uma adição para fora do escopo, a integração é recusada. Depois do cherry-pick, o worker confere os caminhos do commit aplicado antes de executar os testes.
- Uma ferramenta MCP não consegue chamar o Claude por conta própria, mas um comando em segundo plano consegue: a resposta de `submit_plan` e de `resume_run` traz o comando `contracanto --watch --repo <projeto>` da execução, com o caminho completo do executável. Na aba Code, o Claude o roda com `run_in_background` e encerra o turno; quando a execução termina, o comando sai com o resumo e o Claude Code acorda o Claude com o resultado. Código 0: tudo aprovado; 1: alguma tarefa não aprovada; 3: o worker morreu, e `wait_for_run` faz a retomada. O `CLAUDE.md` deste repositório traz essas instruções; copie o parágrafo para o `CLAUDE.md` dos projetos orquestrados.
- Sem comando em segundo plano, `wait_for_run` espera a execução terminar, por até 600 segundos por chamada, e devolve um resumo curto, com a branch de destino em `target`; `get_run_status` devolve o relatório completo, com pareceres, tempos e log. `worker_alive` indica se o worker ainda está rodando: com `false`, o estado não vai mais mudar e a reserva precisa ser liberada pelo operador.
- Para acompanhar ao vivo, rode `contracanto --watch --repo <projeto>` em um terminal: o painel mostra cada tarefa, a fase em andamento (lida do `run.log` do worker), o tempo e os erros, e fecha sozinho quando a execução termina. Ctrl+C fecha só o painel. Na aba Code, o Claude pode rodar o mesmo comando em segundo plano logo após `submit_plan`: sem terminal, ele escreve uma linha por mudança de estado e sai ao fim com o resumo, e o Claude Code avisa o Claude quando o comando termina. O painel não retoma um worker morto; para isso use `wait_for_run`.
- Cada tarefa aprovada vira um commit na branch de destino, com o identificador e a descrição da tarefa no assunto e a execução de origem no corpo. A branch de destino é a que está em uso ou, com `--branch`, uma branch nova.
- No Windows, o servidor MCP roda como descendente do app do Claude e dentro de um Job Object dele, que pode proibir a saída (`CREATE_BREAKAWAY_FROM_JOB`). Lançado diretamente, o worker morreria quando o app encerrasse a árvore de processos ou o job, ao fechar ou reiniciar. Por isso o servidor pede à WMI (`Win32_Process.Create`) que crie um intermediário, `contracanto/detach.py`, fora do job e da árvore do app; ele lança o worker, sem janela, com o ambiente, o diretório e o `run.log` da execução, e termina. O pedido passa por um arquivo temporário do usuário, removido em seguida. Sem a WMI (serviço desativado ou negado por política), o intermediário é criado pelo próprio servidor, o que ainda tira o worker da árvore de processos; o motivo vai para o `run.log` e, se o worker ficar num job, `submit_plan` avisa que ele pode morrer com o app, caso em que `wait_for_run` relança as tarefas que faltarem. Como o worker sobrevive ao app, fechar o Claude não interrompe uma execução: use `stop_run` ou `contracanto --stop`.
- Se o worker morrer sem registrar o estado final (processo encerrado, máquina reiniciada), `wait_for_run` libera a reserva e relança sozinho as tarefas que faltavam, com as mesmas opções, até `CONTRACANTO_AUTO_RESUME` vezes. Ele só faz isso com o repositório limpo, sem operação Git pendente e sem commit de tarefa não aprovada no histórico; caso contrário, `auto_resume.error` no resumo diz o motivo e a recuperação é manual. Pela CLI, `--resume` também libera sozinho a reserva de um worker morto. Uma execução que terminou pedindo recuperação manual nunca é retomada automaticamente.
- Não há limitação de custos/tokens, isolamento forte de execução nem fila transacional. Os comandos de testes e de preparação rodam com as suas permissões: fixe-os com `CONTRACANTO_TEST_COMMAND` e `CONTRACANTO_SETUP_COMMAND` antes de usar em um repositório real.
- Scripts usam revisão por modelo e não garantem correção ou segurança. Execute testes e auditoria adicionais antes de merge de produção.
- Manter o executor (`agy`, ou o Codex em `CONTRACANTO_CODEX_PATH`) e o `claude` no PATH do processo MCP, com autenticação ativa, é obrigatório.

## Estado e recuperação

- Cada execução registra `status.json`, `plan.json`, `options.json` (as opções com que foi iniciada, usadas na retomada automática) e, quando iniciada pelo MCP, `run.log` e `pid.txt` em `.agent-runs/runs/<run_id>/`. `get_run_status()` e `--status` devolvem esse estado como JSON, com horários de início e fim, o tempo gasto por tarefa em cada fase e, quando os CLIs informam, os tokens de cada fase e o total por papel (executor e revisor); veja [docs/recuperacao.md](docs/recuperacao.md).
- O worker registra cada evento da execução, com a hora: lotes, mudanças de estado das tarefas, início e fim de cada fase, vereditos, commits aplicados e erros. Pela CLI os eventos saem na saída de erro; pelo MCP ficam em `run.log`, cujo final aparece em `get_run_status()`.
- Para encerrar a execução ativa, chame `stop_run()` ou execute `contracanto --stop`.
- CLI e MCP compartilham a reserva `.agent-runs/run.lock`. Quando o estado do Git ou dos subprocessos fica incerto, `recovery_required: true` mantém a reserva até a inspeção manual; depois dela, `--unlock` libera a reserva.

O formato do estado, as garantias de cada etapa e o procedimento após encerramento abrupto estão em [docs/recuperacao.md](docs/recuperacao.md).

## Estrutura do código

Instalado, o pacote oferece os comandos `contracanto` e `contracanto-mcp`. `examples/` traz um plano ilustrativo e o plano e a verificação do smoke. Toda a lógica fica no pacote `contracanto/`:

| Módulo | Responsabilidade |
| --- | --- |
| `config.py` | Limites, tempos e parâmetros dos CLIs externos |
| `errors.py` | Exceções que decidem se a reserva pode ser liberada |
| `plan.py` | Formato e validação do plano; regras de escopo de arquivos |
| `state.py` | Workspace, reserva, pedido de parada e `RunState`, único escritor de `status.json` |
| `process.py` | Execução de comandos com contenção da árvore de subprocessos |
| `windows_job.py` | Job Object usado pela contenção no Windows |
| `gitops.py` | Consultas e verificações Git: limpeza, escopo, branch e HEAD |
| `agents.py` | Chamadas ao executor, `agy` ou `codex` (implementação e correção), e ao `claude` (revisão) |
| `runner.py` | Ciclo da execução: lotes, revisão, integração e rollback |
| `maintenance.py` | Consulta de status, destravamento da reserva, retomada e poda de execuções |
| `server.py` | Ferramentas MCP e lançamento do worker |
| `cli.py`, `__main__.py` | Linha de comando (`contracanto` ou `python -m contracanto`) |

Nenhum módulo guarda estado global: o repositório e o diretório de execuções chegam por um `Workspace`, e cada execução carrega um `RunContext`. Os testes criam workspaces e repositórios temporários com `tests/support.py`. `.gitattributes` fixa o fim de linha em LF em todas as plataformas.

## Dependências e validação de alterações

`pyproject.toml` é a única declaração das dependências diretas (`mcp` e, no Windows, `pywin32`) e das ferramentas de desenvolvimento (`ruff` e `mypy`, no extra `dev`), além do pacote e de seus comandos. `requirements.lock.txt` fixa também as transitivas do ambiente validado; use-o na instalação e na CI. Para desenvolver, instale o lock e depois `python -m pip install -e ".[dev]"`. A CI confere com `scripts/check_lock.py` que o lock fixa as mesmas versões declaradas em `pyproject.toml` e executa `ruff check .`, `ruff format --check .` e `mypy` além dos testes, pelo executor paralelo. O `mypy` confere o pacote, `scripts/` e `tests/`. A versão do pacote fica em `contracanto/__init__.py`. Atualizações devem ser intencionais: regenere as versões em ambiente limpo, execute `pip check`, a suíte completa e o smoke em cópia descartável. O workflow não chama modelos nem valida autenticação.

A autenticação no aplicativo Claude e a execução real com os modelos devem ser verificadas na máquina do operador. Os tokens e o custo estimado são registrados, mas não há orçamento nem limite de tokens ou de custo. A retomada automática cobre o worker que morre com o repositório íntegro; quando o Git ou os subprocessos ficam em estado incerto, a reserva e a recuperação manual continuam obrigatórias.

## Licença

[MIT](LICENSE).
