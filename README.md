# Contracanto

Claude planeja e revisa. Antigravity implementa cada tarefa em um worktree Git. Um servidor MCP em Python coordena os dois e integra as mudanças após a revisão e os testes.

**Protótipo de integração local para Windows**, com regressões automatizadas e smoke local anterior. Cada máquina precisa validar suas credenciais e permissões. Não é solução de produção.

## Pré-requisitos
- Windows, Python 3.13–3.15 e Git for Windows com suporte a `git merge-tree --write-tree` para integrar tarefas paralelas. Ambiente local validado: Python 3.15.2, MCP 1.30.0, `agy` 1.5.8 e Claude Code 2.1.325. A matriz de CI está configurada para Python 3.13 e 3.15 em Windows e Linux; confira o resultado do workflow antes de adotar outra combinação.
- Aplicativo Claude Desktop (aba Code, sessão **Local**)
- Claude Code CLI instalado e autenticado (`claude --version`, `claude -p "oi"`)
- Antigravity App e CLI (`agy --version`), com autenticação realizada interativamente
- O repositório deve estar limpo; execute tudo em projeto de teste

Instale o CLI do Antigravity seguindo a documentação oficial https://antigravity.google/docs/getting-started . A instalação Windows documentada é:

```powershell
irm https://antigravity.google/cli/install.ps1 | iex
agy --version
agy
```

## Preparação
1. Copie os arquivos deste pacote, incluindo o diretório `contracanto/`, para **a raiz do repositório Git** que deseja modificar. O projeto orquestrado é o diretório que contém `contracanto/`, `orchestrator.py` e `mcp_server.py`. Confirme com `git status` que as únicas modificações são os novos arquivos e faça um commit inicial.
2. No PowerShell, dentro do projeto:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock.txt
python -m pip check
claude --version
agy --version
```

3. Para orquestrar **outro repositório** sem copiar os arquivos, mantenha esta instalação onde está e informe o destino com `--repo CAMINHO` na CLI ou com a variável `CONTRACANTO_REPO` no ambiente do servidor MCP. O estado das execuções fica em `.agent-runs/` dentro do repositório de destino, que precisa ignorar esse diretório (por exemplo em `.git/info/exclude`) para continuar limpo.
4. `.venv/`, `.agent-runs/` e a configuração local `.mcp.json` já estão no `.gitignore`. Se estiver começando a partir de um ZIP, inicialize o Git e crie o primeiro commit:

```powershell
git init -b main
git add .
git commit -m "chore: initialize orchestrator"
git status --porcelain
```

O último comando deve retornar vazio. Configure `git config user.name` e `git config user.email` caso o Git solicite sua identidade.

Como alternativa a copiar os arquivos, instale o pacote em um venv: `python -m pip install CAMINHO_DO_CONTRACANTO`. Isso cria os comandos `contracanto` (CLI) e `contracanto-mcp` (servidor MCP). Instalado, o pacote orquestra o repositório do diretório atual, salvo `--repo` ou `CONTRACANTO_REPO`. Com os arquivos copiados para o projeto, `py orchestrator.py` e `python -m contracanto` são equivalentes.
5. O executor e todas as correções passam explicitamente `--mode auto --sandbox` ao Antigravity, sem `--dangerously-skip-permissions`. Configure uma allowlist limitada no CLI antes de operar. Comandos negados devem ser autorizados de forma granular; não desative o sandbox para contornar a falha. O modo headless pode negar comandos não autorizados. Configure permissões granulares conforme docs: https://antigravity.google/docs/cli/headless/

## Teste sem MCP
Edite `plan.example.json` com tasks reais e arquivos autorizados. Use um repositório descartável. Exemplo:

```powershell
py orchestrator.py plan.example.json --parallel 3 --retries 2 --test py -m pytest
```

O plano de exemplo é ilustrativo; adaptações são necessárias conforme o stack. Sem pytest instalado, configure um comando válido antes de executar. Para um primeiro teste, use --parallel 1.

`--test` deve ser a **última opção**: todos os argumentos seguintes pertencem ao comando de testes, inclusive `-m`, `-v` e outras flags. `py orchestrator.py --help` lista todas as opções. `--repo CAMINHO` escolhe o repositório a orquestrar e `--keep-worktrees` preserva worktree e branch das tarefas aprovadas.

## Operação pela linha de comando

| Comando | Efeito |
| --- | --- |
| `py orchestrator.py --status` | Mostra, em JSON, o estado da execução ativa ou da mais recente (o mesmo de `get_run_status`) |
| `py orchestrator.py --stop` | Pede o encerramento cooperativo da execução ativa |
| `py orchestrator.py --unlock` | Remove a reserva de um worker que já terminou, depois de conferir o processo e o Git |
| `py orchestrator.py --prune-runs N` | Remove os registros das execuções encerradas, mantendo as N mais recentes |

Todos aceitam `--repo CAMINHO`. `--unlock` recusa, com código de saída 1, enquanto o worker da execução estiver vivo, houver operação Git pendente ou o repositório tiver alterações. `--prune-runs` nunca remove a execução reservada, uma sem estado final ou uma com recuperação pendente; também mantém a execução inteira quando algum worktree dela tem alterações.

Os tempos limite e o modelo do revisor são ajustáveis por variáveis de ambiente, em segundos: `CONTRACANTO_TEST_TIMEOUT` (padrão 600), `CONTRACANTO_AGENT_TIMEOUT` (1600), `CONTRACANTO_REVIEW_TIMEOUT` (900), `CONTRACANTO_GIT_TIMEOUT` (180), `CONTRACANTO_COMMAND_TIMEOUT` (1800) e `CONTRACANTO_REVIEW_MODEL` (`sonnet`). No MCP, defina-as em `"env"` na configuração do servidor; o worker herda o ambiente. Um valor inválido interrompe a inicialização com o nome da variável.

## Testes deste projeto

Os testes usam `unittest`, incluído no Python, e Git local. Não chamam os CLIs dos modelos nem consomem tokens. O executor paralelo roda cada teste em um processo próprio, com um processo por núcleo por padrão:

```powershell
.\.venv\Scripts\python.exe scripts/run_tests.py
```

`-j N` limita os processos, `-v` mostra cada teste com o tempo gasto e um texto qualquer seleciona os testes cujo identificador o contém (por exemplo `scripts/run_tests.py test_scope_gates`). `python -m unittest discover -s tests -v` continua funcionando, de forma serial.

Há regressões de parsing da CLI, IDs que diferem apenas por maiúsculas/minúsculas, escopo com Unicode/espaços/renomeações, prompts grandes por stdin, estado terminal, reserva de execução e rollback. Há testes com Git e subprocessos reais para encerramento de descendentes por timeout, execução paralela, dependências e rejeição seguida de correção e integração. Os modelos são simulados; os testes de recuperação também injetam falhas operacionais, incluindo erros de leitura do Git após os testes de integração. A CI executa a suíte e `pip check`. No Windows, os testes cobrem também encerramento após a saída do processo pai, indisponibilidade de `taskkill`, falha de associação ao Job Object e isolamento entre comandos paralelos.

Para testar os CLIs reais, use uma cópia descartável deste projeto com Git limpo, autenticações ativas e permissões adequadas. O smoke cria apenas `smoke.txt` e **consome chamadas dos modelos**:

```powershell
.\.venv\Scripts\python.exe orchestrator.py plan.smoke.json --parallel 1 --retries 0 --test python smoke_check.py
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
  "test_command_json": "[\"python\",\"smoke_check.py\"]"
}
```

Quem fornece `--test` ou `test_command_json` escolhe um comando local. O worker recebe uma lista não vazia de argumentos e inicia o executável diretamente. O comando e o código que ele carrega rodam fora do sandbox do Antigravity, com as permissões do usuário, inclusive acesso à rede, às credenciais e a arquivos fora do repositório. Podem fazer push ou deploy. Use apenas comandos, planos e código de origem confiável. A primeira execução ocorre no worktree antes do parecer do revisor. Cada nova revisão repete o teste, e a integração o executa novamente na branch de destino.

`parallel` aceita 1 a 8. `retries` aceita 0 a 3 e controla as tentativas de correção após rejeição da revisão.

Se os testes do worktree falharem, o executor recebe o parecer do revisor, o código de saída e o trecho final da saída dos testes na tentativa de correção. Se as tentativas se esgotarem, consulte o diagnóstico em `details.<id>.review` e o resumo em `details.<id>.error` no status da tarefa.

O comando de testes deve preservar o commit atual, a branch ativa (ou o estado de HEAD destacado) e deixar o Git limpo. Trocar de branch mesmo mantendo o mesmo commit, alterar arquivos rastreados ou criar arquivos não ignorados impede a aprovação, mesmo com código de saída 0. Artefatos ignorados pelo Git, como caches, são permitidos. No worktree, a tarefa falha e as alterações ficam disponíveis para inspeção. Na branch de destino, mudanças de branch, alterações ou commits feitos pelos testes exigem recuperação manual: a reserva é mantida e nenhuma outra tarefa integra, inclusive se o comando falhar ou sofrer timeout. Não há limpeza destrutiva automática desses arquivos.

Merge, cherry-pick, revert, rebase, `git am` e bisect em andamento também impedem a aprovação, inclusive quando `git status --porcelain` está vazio. Se os testes de integração deixarem uma dessas operações aberta na branch de destino, a reserva permanece ativa para recuperação manual.

Para arquivos rastreados com `assume-unchanged` ou `skip-worktree`, o worker compara o conteúdo em disco com o HEAD usando um índice Git temporário. A verificação preserva as marcas do índice local e alcança os submódulos inicializados. Em um checkout parcial, o worker usa `git sparse-checkout check-rules` para reconhecer arquivos ausentes conforme as regras de seleção.

A verificação de limpeza inclui submódulos inicializados mesmo quando configurados com `ignore = all`. Antes do cherry-pick, o worker verifica se a integração sobrescreveria dados locais não rastreados, inclusive arquivos ignorados. Uma colisão faz a tarefa falhar antes de alterar a branch. Se a colisão surgir no caminho de um revert após os testes, os dados ficam preservados e a reserva permanece ativa para recuperação manual.

## Conectar ao Claude Desktop na aba Code
Edite o `.mcp.json` **na raiz do repositório**, usando o caminho absoluto do Python do venv e o caminho absoluto do servidor. Exemplo (substituir USUARIO e PROJETO):

O arquivo local não é versionado. Use `.mcp.example.json` como modelo em outra máquina.

```json
{
  "mcpServers": {
    "antigravity-orchestrator": {
      "command": "C:\\Users\\USUARIO\\PROJETO\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\USUARIO\\PROJETO\\mcp_server.py"]
    }
  }
}
```

Com o pacote instalado, use `"command": "C:\\CAMINHO\\DO\\VENV\\Scripts\\contracanto-mcp.exe"` sem `args`.

Para orquestrar um repositório diferente daquele onde o Contracanto está instalado, acrescente ao servidor `"env": {"CONTRACANTO_REPO": "C:\\Users\\USUARIO\\OUTRO_PROJETO"}`.

Alternativamente, configure o MCP no ambiente local do Claude Code. O MCP de Chat e o da aba Code não são os mesmos. Reinicie a sessão local do Claude e verifique se aparecem as ferramentas `submit_plan`, `stop_run`, `get_run_status` e `get_submitted_plan`.

Peça ao Claude na aba Code:

> Inspecione este repositório, proponha um plano em tarefas pequenas e verificáveis com dependências e arquivos autorizados; use submit_plan para iniciar execução com 3 workers e testes `py -m pytest`. Revise as tasks automaticamente via Claude CLI. Não faça deploy/push ou modificações destrutivas. Consulte get_run_status ao final.

## Comportamento e limitações
- O Claude cria o grafo de tarefas e envia pelo MCP. A fila é executada por Python, chamando `agy` em worktrees locais isolados.
- O Claude Code CLI revisa o patch usando o alias `sonnet` (ou `CONTRACANTO_REVIEW_MODEL`), sem ferramentas internas e sem carregar servidores MCP. O revisor responde com um objeto JSON (`verdict`, `summary`, `problems`); o orquestrador aceita esse objeto mesmo cercado de texto ou de uma cerca de código, e também a resposta no formato anterior, com `APPROVED` ou `REJECTED` na primeira linha. Qualquer outra resposta conta como rejeição, e a aprovação exige também que os testes do worktree passem. O revisor recebe os diffs de texto e os patches binários no formato aplicável do Git. Para um critério que depende do conteúdo de uma imagem ou de outro binário, inclua um teste específico desse conteúdo. Prompts seguem por stdin para evitar o limite de argumentos do Windows. A aprovação exige o veredito `APPROVED` na primeira linha e testes aprovados.
- Tarefas independentes, que não anunciam ownership conflitante, podem rodar em paralelo. Implementação, revisão e correções de um lote rodam em paralelo, cada tarefa no próprio worktree; a integração na branch de destino é sequencial, na ordem em que as implementações terminaram. Se uma revisão exigir recuperação manual, nenhuma tarefa do lote é integrada.
- No pedido ao revisor, `patch_encoding` identifica a codificação usada para representar os bytes do patch. Codificar o campo `patch` com esse valor reproduz a saída original do Git, inclusive quebras de linha e acentos de arquivos legados.
- Após cada implementação ou correção, o orquestrador confere a branch atribuída ao worktree e o commit esperado antes de registrar as mudanças. Uma troca de branch no mesmo commit também faz a tarefa falhar. O escopo é conferido pelo diff completo entre a base e o candidato antes de cada revisão, incluindo origem e destino de renomeações.
- A integração termina na branch local. Depois de integrada e aprovada, a tarefa tem seu worktree e sua branch `agent/*` removidos; `details.<id>.worktree_removed` registra a limpeza e `cleanup_error` uma eventual falha, que não altera o resultado. Use `--keep-worktrees` (ou `keep_worktrees` em `submit_plan`) para preservá-los. Tarefas que falham, inclusive por conflito de cherry-pick, sempre mantêm worktree e branch para inspeção.
- Antes do cherry-pick, o worker compara os caminhos da integração prevista com a lista `files` da tarefa. Se uma tarefa anterior moveu um diretório e o Git redirecionaria uma adição para fora do escopo, a integração é recusada. Depois do cherry-pick, o worker confere os caminhos do commit aplicado antes de executar os testes.
- `get_run_status` precisa ser consultado; o aplicativo Claude não recebe notificação push de término.
- Não há retomada após desligar o processo, limitação de custos/tokens, isolamento forte de execução ou fila transacional. Não use em repositórios reais até adicionar esses recursos.
- Scripts usam revisão por modelo e não garantem correção ou segurança. Execute testes e auditoria adicionais antes de merge de produção.
- Manter `agy` e `claude` no PATH do processo MCP e ter autenticação ativa é obrigatório.

## Estado e recuperação

- Cada execução registra `status.json`, `plan.json` e, quando iniciada pelo MCP, `run.log` e `pid.txt` em `.agent-runs/runs/<run_id>/`. `get_run_status()` e `--status` devolvem esse estado como JSON, com horários de início e fim e o tempo gasto por tarefa em cada fase.
- Para encerrar a execução ativa, chame `stop_run()` ou execute `py orchestrator.py --stop`.
- CLI e MCP compartilham a reserva `.agent-runs/run.lock`. Quando o estado do Git ou dos subprocessos fica incerto, `recovery_required: true` mantém a reserva até a inspeção manual; depois dela, `--unlock` libera a reserva.

O formato do estado, as garantias de cada etapa e o procedimento após encerramento abrupto estão em [docs/recuperacao.md](docs/recuperacao.md).

## Estrutura do código

`orchestrator.py` (CLI) e `mcp_server.py` (servidor MCP) são atalhos para quem copia os arquivos para o projeto; instalado, o pacote oferece os comandos `contracanto` e `contracanto-mcp`. Toda a lógica fica no pacote `contracanto/`:

| Módulo | Responsabilidade |
| --- | --- |
| `config.py` | Limites, tempos e parâmetros dos CLIs externos |
| `errors.py` | Exceções que decidem se a reserva pode ser liberada |
| `plan.py` | Formato e validação do plano; regras de escopo de arquivos |
| `state.py` | Workspace, reserva, pedido de parada e `RunState`, único escritor de `status.json` |
| `process.py` | Execução de comandos com contenção da árvore de subprocessos |
| `windows_job.py` | Job Object usado pela contenção no Windows |
| `gitops.py` | Consultas e verificações Git: limpeza, escopo, branch e HEAD |
| `agents.py` | Chamadas ao `agy` (implementação e correção) e ao `claude` (revisão) |
| `runner.py` | Ciclo da execução: lotes, revisão, integração e rollback |
| `maintenance.py` | Consulta de status, destravamento da reserva e poda de execuções |
| `server.py` | Ferramentas MCP e lançamento do worker |
| `cli.py`, `__main__.py` | Linha de comando (`contracanto` ou `python -m contracanto`) |

Nenhum módulo guarda estado global: o repositório e o diretório de execuções chegam por um `Workspace`, e cada execução carrega um `RunContext`. Os testes criam workspaces e repositórios temporários com `tests/support.py`. `.gitattributes` fixa o fim de linha em LF em todas as plataformas.

## Dependências e validação de alterações

`requirements.txt` fixa as dependências diretas; `requirements.lock.txt` fixa também as transitivas do ambiente validado, com `pywin32` restrito ao Windows. Use o lock na instalação e na CI. `requirements-dev.txt` traz o `ruff` e o `mypy`; a CI executa `ruff check .`, `ruff format --check .` e `mypy` além dos testes, pelo executor paralelo. `pyproject.toml` também declara o pacote e seus comandos para `pip install`. Atualizações devem ser intencionais: regenere as versões em ambiente limpo, execute `pip check`, a suíte completa e o smoke em cópia descartável. O workflow não chama modelos nem valida autenticação.

A autenticação no aplicativo Claude e a execução real com os modelos devem ser verificadas na máquina do operador. Retomada automática após crash e orçamento de tokens/custo continuam fora do escopo deste protótipo; a reserva e a recuperação manual continuam obrigatórias nesses casos.
