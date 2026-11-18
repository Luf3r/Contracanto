# Contracanto

Claude planeja e revisa. Antigravity implementa cada tarefa em um worktree Git. Um servidor MCP em Python coordena os dois e integra as mudanças após a revisão e os testes.

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
3. O estado das execuções fica em `.agent-runs/` dentro desse repositório, que precisa ignorar o diretório para continuar limpo: acrescente `.agent-runs/` ao `.gitignore` ou a `.git/info/exclude`.
4. O executor e todas as correções passam explicitamente `--mode auto --sandbox` ao Antigravity, sem `--dangerously-skip-permissions`. Configure uma allowlist limitada no CLI antes de operar. Comandos negados devem ser autorizados de forma granular; não desative o sandbox para contornar a falha. O modo headless pode negar comandos não autorizados. Configure permissões granulares conforme docs: https://antigravity.google/docs/cli/headless/

Sem instalar, `python -m contracanto` e `python -m contracanto.server` equivalem a `contracanto` e `contracanto-mcp`, desde que o diretório desta cópia esteja no `PYTHONPATH`. Para desenvolver o próprio Contracanto, instale-o no venv dele com `python -m pip install -e ".[dev]"`.

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

Erros de uso e de preparação, como plano inexistente, repositório com alterações ou valor inválido em uma variável `CONTRACANTO_*`, saem em uma linha iniciada por `Erro:`, com código de saída diferente de zero. As opções de execução (`--test`, `--setup-json`, `--branch`, `--keep-worktrees`) são recusadas junto de `--status`, `--watch`, `--stop`, `--unlock` e `--prune-runs`. Pelo MCP, `submit_plan` e `resume_run` conferem o repositório antes de reservar a execução: alterações pendentes, HEAD destacado sem `branch` e nome de branch inválido ou já usado voltam como `Erro:` na própria chamada. Antes de chamar qualquer agente, o worker confere se os executáveis dos comandos de testes e de preparação existem. No Windows, um script `.cmd` ou `.bat` encontrado no PATH, como `npm`, é chamado pelo caminho completo.

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
| `CONTRACANTO_REVIEW_TIMEOUT` | 900 | Segundos para cada chamada ao revisor |
| `CONTRACANTO_GIT_TIMEOUT` | 180 | Segundos para cada comando Git |
| `CONTRACANTO_REVIEW_MODEL` | `sonnet` | Modelo usado pelo revisor |
| `CONTRACANTO_AGENT_MODEL` | sem valor | Modelo do executor, passado ao `agy` em `--model` (ids em `agy models`, por exemplo `gemini-4-hydrogen-high`); sem valor, vale o padrão do `agy` |
| `CONTRACANTO_AGENT_RETRIES` | 1 | Novas chamadas ao `agy` quando uma implementação ou correção falha (0 a 3) |
| `CONTRACANTO_AUTO_RESUME` | 1 | Vezes que `wait_for_run` relança sozinho uma execução cujo worker morreu (0 a 3; 0 desliga) |
| `CONTRACANTO_REVIEW_PATCH_LIMIT` | 400000 | Tamanho máximo do patch enviado ao revisor, em caracteres |
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

> Inspecione este repositório, proponha um plano em tarefas pequenas e verificáveis com dependências e arquivos autorizados; use submit_plan para iniciar execução com 3 workers e testes `py -m pytest`. Revise as tasks automaticamente via Claude CLI. Não faça deploy/push ou modificações destrutivas. Acompanhe com wait_for_run e consulte get_run_status ao final.

## Comportamento e limitações
- O Claude cria o grafo de tarefas e envia pelo MCP. A fila é executada por Python, chamando `agy` em worktrees locais isolados.
- O Claude Code CLI revisa o patch usando o alias `sonnet` (ou `CONTRACANTO_REVIEW_MODEL`), sem ferramentas internas e sem carregar servidores MCP. Ele roda em um diretório temporário vazio, fora do worktree, para não carregar o `CLAUDE.md` nem outras instruções do projeto revisado; tudo o que ele avalia chega pelo pedido. O revisor responde com um objeto JSON (`verdict`, `summary`, `problems`); o orquestrador aceita esse objeto mesmo cercado de texto ou de uma cerca de código. Qualquer outra resposta, inclusive `APPROVED` fora de um objeto JSON, conta como rejeição, e a aprovação exige também que os testes do worktree passem. O revisor recebe os diffs de texto e os patches binários no formato aplicável do Git. Para um critério que depende do conteúdo de uma imagem ou de outro binário, inclua um teste específico desse conteúdo. Prompts seguem por stdin para evitar o limite de argumentos do Windows. Um patch maior que `CONTRACANTO_REVIEW_PATCH_LIMIT` faz a tarefa falhar com essa indicação, em vez de ser revisado pela metade; divida a tarefa.
- Tarefas independentes, que não anunciam ownership conflitante, podem rodar em paralelo. Implementação, revisão e correções de um lote rodam em paralelo, cada tarefa no próprio worktree; a integração na branch de destino é sequencial, na ordem em que as implementações terminaram. Se uma revisão exigir recuperação manual, nenhuma tarefa do lote é integrada. Quando uma tarefa conflita com outra já integrada no mesmo lote, ela falha e `details.<id>.error` lista os arquivos em conflito.
- No pedido ao revisor, `patch_encoding` identifica a codificação usada para representar os bytes do patch. Codificar o campo `patch` com esse valor reproduz a saída original do Git, inclusive quebras de linha e acentos de arquivos legados.
- Após cada implementação ou correção, o orquestrador confere a branch atribuída ao worktree e o commit esperado antes de registrar as mudanças. Uma troca de branch no mesmo commit também faz a tarefa falhar. O escopo é conferido pelo diff completo entre a base e o candidato antes de cada revisão, incluindo origem e destino de renomeações.
- A integração termina na branch local. Depois de integrada e aprovada, a tarefa tem seu worktree e sua branch `agent/*` removidos; `details.<id>.worktree_removed` registra a limpeza e `cleanup_error` uma eventual falha, que não altera o resultado. Use `--keep-worktrees` (ou `keep_worktrees` em `submit_plan`) para preservá-los. Tarefas que falham, inclusive por conflito de cherry-pick, sempre mantêm worktree e branch para inspeção.
- Antes do cherry-pick, o worker compara os caminhos da integração prevista com a lista `files` da tarefa. Se uma tarefa anterior moveu um diretório e o Git redirecionaria uma adição para fora do escopo, a integração é recusada. Depois do cherry-pick, o worker confere os caminhos do commit aplicado antes de executar os testes.
- O aplicativo Claude não recebe notificação push de término. `wait_for_run` espera a execução terminar, por até 600 segundos por chamada, e devolve um resumo curto, com a branch de destino em `target`; `get_run_status` devolve o relatório completo, com pareceres, tempos e log. `worker_alive` indica se o worker ainda está rodando: com `false`, o estado não vai mais mudar e a reserva precisa ser liberada pelo operador.
- Para acompanhar ao vivo, rode `contracanto --watch --repo <projeto>` em um terminal: o painel mostra cada tarefa, a fase em andamento (lida do `run.log` do worker), o tempo e os erros, e fecha sozinho quando a execução termina. Ctrl+C fecha só o painel. Na aba Code, o Claude pode rodar o mesmo comando em segundo plano logo após `submit_plan`: sem terminal, ele escreve uma linha por mudança de estado e sai ao fim com o resumo, e o Claude Code avisa o Claude quando o comando termina. O painel não retoma um worker morto; para isso use `wait_for_run`.
- Cada tarefa aprovada vira um commit na branch de destino, com o identificador e a descrição da tarefa no assunto e a execução de origem no corpo. A branch de destino é a que está em uso ou, com `--branch`, uma branch nova.
- Se o worker morrer sem registrar o estado final (processo encerrado, máquina reiniciada), `wait_for_run` libera a reserva e relança sozinho as tarefas que faltavam, com as mesmas opções, até `CONTRACANTO_AUTO_RESUME` vezes. Ele só faz isso com o repositório limpo, sem operação Git pendente e sem commit de tarefa não aprovada no histórico; caso contrário, `auto_resume.error` no resumo diz o motivo e a recuperação é manual. Pela CLI, `--resume` também libera sozinho a reserva de um worker morto. Uma execução que terminou pedindo recuperação manual nunca é retomada automaticamente.
- Não há limitação de custos/tokens, isolamento forte de execução nem fila transacional. Os comandos de testes e de preparação rodam com as suas permissões: fixe-os com `CONTRACANTO_TEST_COMMAND` e `CONTRACANTO_SETUP_COMMAND` antes de usar em um repositório real.
- Scripts usam revisão por modelo e não garantem correção ou segurança. Execute testes e auditoria adicionais antes de merge de produção.
- Manter `agy` e `claude` no PATH do processo MCP e ter autenticação ativa é obrigatório.

## Estado e recuperação

- Cada execução registra `status.json`, `plan.json`, `options.json` (as opções com que foi iniciada, usadas na retomada automática) e, quando iniciada pelo MCP, `run.log` e `pid.txt` em `.agent-runs/runs/<run_id>/`. `get_run_status()` e `--status` devolvem esse estado como JSON, com horários de início e fim e o tempo gasto por tarefa em cada fase.
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
| `agents.py` | Chamadas ao `agy` (implementação e correção) e ao `claude` (revisão) |
| `runner.py` | Ciclo da execução: lotes, revisão, integração e rollback |
| `maintenance.py` | Consulta de status, destravamento da reserva, retomada e poda de execuções |
| `server.py` | Ferramentas MCP e lançamento do worker |
| `cli.py`, `__main__.py` | Linha de comando (`contracanto` ou `python -m contracanto`) |

Nenhum módulo guarda estado global: o repositório e o diretório de execuções chegam por um `Workspace`, e cada execução carrega um `RunContext`. Os testes criam workspaces e repositórios temporários com `tests/support.py`. `.gitattributes` fixa o fim de linha em LF em todas as plataformas.

## Dependências e validação de alterações

`pyproject.toml` é a única declaração das dependências diretas (`mcp` e, no Windows, `pywin32`) e das ferramentas de desenvolvimento (`ruff` e `mypy`, no extra `dev`), além do pacote e de seus comandos. `requirements.lock.txt` fixa também as transitivas do ambiente validado; use-o na instalação e na CI. Para desenvolver, instale o lock e depois `python -m pip install -e ".[dev]"`. A CI confere com `scripts/check_lock.py` que o lock fixa as mesmas versões declaradas em `pyproject.toml` e executa `ruff check .`, `ruff format --check .` e `mypy` além dos testes, pelo executor paralelo. O `mypy` confere o pacote, `scripts/` e `tests/`. A versão do pacote fica em `contracanto/__init__.py`. Atualizações devem ser intencionais: regenere as versões em ambiente limpo, execute `pip check`, a suíte completa e o smoke em cópia descartável. O workflow não chama modelos nem valida autenticação.

A autenticação no aplicativo Claude e a execução real com os modelos devem ser verificadas na máquina do operador. Orçamento de tokens/custo continua fora do escopo. A retomada automática cobre o worker que morre com o repositório íntegro; quando o Git ou os subprocessos ficam em estado incerto, a reserva e a recuperação manual continuam obrigatórias.

## Licença

[MIT](LICENSE).
