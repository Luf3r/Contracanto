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

3. `.venv/`, `.agent-runs/` e a configuração local `.mcp.json` já estão no `.gitignore`. Se estiver começando a partir de um ZIP, inicialize o Git e crie o primeiro commit:

```powershell
git init -b main
git add .
git commit -m "chore: initialize orchestrator"
git status --porcelain
```

O último comando deve retornar vazio. Configure `git config user.name` e `git config user.email` caso o Git solicite sua identidade.
4. O executor e todas as correções passam explicitamente `--mode auto --sandbox` ao Antigravity, sem `--dangerously-skip-permissions`. Configure uma allowlist limitada no CLI antes de operar. Comandos negados devem ser autorizados de forma granular; não desative o sandbox para contornar a falha. O modo headless pode negar comandos não autorizados. Configure permissões granulares conforme docs: https://antigravity.google/docs/cli/headless/

## Teste sem MCP
Edite `plan.example.json` com tasks reais e arquivos autorizados. Use um repositório descartável. Exemplo:

```powershell
py orchestrator.py plan.example.json --parallel 3 --retries 2 --test py -m pytest
```

O plano de exemplo é ilustrativo; adaptações são necessárias conforme o stack. Sem pytest instalado, configure um comando válido antes de executar. Para um primeiro teste, use --parallel 1.

`--test` deve ser a **última opção**: todos os argumentos seguintes pertencem ao comando de testes, inclusive `-m`, `-v` e outras flags.

## Testes deste projeto

Os testes usam `unittest`, incluído no Python, e Git local. Não chamam os CLIs dos modelos nem consomem tokens:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

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

Alternativamente, configure o MCP no ambiente local do Claude Code. O MCP de Chat e o da aba Code não são os mesmos. Reinicie a sessão local do Claude e verifique se aparecem as ferramentas `submit_plan`, `stop_run`, `get_run_status` e `get_submitted_plan`.

Peça ao Claude na aba Code:

> Inspecione este repositório, proponha um plano em tarefas pequenas e verificáveis com dependências e arquivos autorizados; use submit_plan para iniciar execução com 3 workers e testes `py -m pytest`. Revise as tasks automaticamente via Claude CLI. Não faça deploy/push ou modificações destrutivas. Consulte get_run_status ao final.

## Comportamento e limitações
- O Claude cria o grafo de tarefas e envia pelo MCP. A fila é executada por Python, chamando `agy` em worktrees locais isolados.
- O Claude Code CLI revisa o patch usando o alias `sonnet`, sem ferramentas internas e sem carregar servidores MCP. O revisor recebe os diffs de texto e os patches binários no formato aplicável do Git. Para um critério que depende do conteúdo de uma imagem ou de outro binário, inclua um teste específico desse conteúdo. Prompts seguem por stdin para evitar o limite de argumentos do Windows. A aprovação exige o veredito `APPROVED` na primeira linha e testes aprovados.
- Tarefas independentes, que não anunciam ownership conflitante, podem rodar em paralelo; a integração ocorre sequencialmente.
- No pedido ao revisor, `patch_encoding` identifica a codificação usada para representar os bytes do patch. Codificar o campo `patch` com esse valor reproduz a saída original do Git, inclusive quebras de linha e acentos de arquivos legados.
- Após cada implementação ou correção, o orquestrador confere a branch atribuída ao worktree e o commit esperado antes de registrar as mudanças. Uma troca de branch no mesmo commit também faz a tarefa falhar. O escopo é conferido pelo diff completo entre a base e o candidato antes de cada revisão, incluindo origem e destino de renomeações.
- A integração termina na branch local. Uma falha de cherry-pick interrompe a tarefa e os worktrees ficam disponíveis para inspeção.
- Antes do cherry-pick, o worker compara os caminhos da integração prevista com a lista `files` da tarefa. Se uma tarefa anterior moveu um diretório e o Git redirecionaria uma adição para fora do escopo, a integração é recusada. Depois do cherry-pick, o worker confere os caminhos do commit aplicado antes de executar os testes.
- `get_run_status` precisa ser consultado; o aplicativo Claude não recebe notificação push de término.
- Não há retomada após desligar o processo, limitação de custos/tokens, isolamento forte de execução ou fila transacional. Não use em repositórios reais até adicionar esses recursos.
- Scripts usam revisão por modelo e não garantem correção ou segurança. Execute testes e auditoria adicionais antes de merge de produção.
- Manter `agy` e `claude` no PATH do processo MCP e ter autenticação ativa é obrigatório.

## Estado e recuperação

Para encerrar o plano ativo pelo MCP, chame `stop_run()`. Você também pode passar o `run_id` obtido em `get_run_status()` para selecionar uma execução específica. Pelo terminal, execute `py orchestrator.py --stop` na raiz do projeto, em outra janela. As duas opções servem para planos iniciados pela CLI ou pelo MCP.

O pedido retorna imediatamente. Em `get_run_status()`, `reservation.stop_requested` fica `true` enquanto o worker encerra os comandos de modelo e testes. Operações Git em andamento chegam a um ponto seguro. Se o cancelamento interromper os testes de integração, o worker verifica o repositório e reverte o commit em teste quando for seguro. Tarefas já aprovadas mantêm seus commits. Consulte `get_run_status()` ou `.agent-runs/runs/<run_id>/status.json` para confirmar o estado final `cancelled`. Se o plano já tiver terminado, vale o resultado registrado. Um encerramento incerto continua exigindo recuperação manual.

CLI e MCP compartilham uma reserva exclusiva em `.agent-runs/run.lock`, criada antes de gravar o plano ou iniciar o processo. Uma segunda execução é recusada. A reserva é liberada ao término normal e em erros tratados com estado conhecido. Em falhas de recuperação Git ou encerramento incerto de subprocessos, `recovery_required: true` preserva a reserva para impedir novas execuções até inspeção manual. Interrupções abruptas também podem preservá-la.

O status expõe o identificador público da execução. O worker usa uma credencial de uso único para assumir a reserva. Repetir a chamada do worker preserva o plano, o estado e a reserva da execução ativa.

`get_run_status()` retorna um objeto JSON com quatro campos:

- `run`: o conteúdo de `status.json` da execução reservada ou, sem reserva, da mais recente; `null` se ela ainda não publicou estado.
- `reservation_state`: `active`, `none` ou `incomplete` (reserva em criação).
- `reservation`: `run_id`, `pid` e `stop_requested` da reserva ativa, ou `null`.
- `log`: as últimas linhas do log do worker lançado pelo MCP.

Se a reserva for removida durante a consulta, `reservation_state` volta a `none`. `submit_plan` responde com um texto iniciado por `Erro:` quando a entrada é inválida ou já existe execução reservada, sem iniciar nada; falhas ao lançar o worker são reportadas como erro da ferramenta. Se não conseguir gravar `pid.txt` após iniciar o worker, `submit_plan` confirma o início e retorna um aviso com o PID correto; a execução continua e a reserva permanece sob controle do worker. Nesse caso, `pid.txt` pode estar ausente ou incompleto: use o PID da resposta para inspeção.

As atualizações de estado substituem o JSON de forma atômica. Se uma consulta no Windows bloquear a substituição do JSON ou a remoção de `run.lock`, o processo tenta novamente por até um segundo. Antes de cada tentativa de remoção, ele confere a identidade da reserva. Se a primeira gravação de `starting` falhar, o diagnóstico da nova execução usa listas de tarefas e detalhes vazias. Uma recuperação já exigida mantém a reserva mesmo se houver erro ao salvar o estado final ou ao ler o estado da execução em curso.

O estado global final é `completed` quando todas as tarefas são aprovadas, `cancelled` quando o worker confirma o encerramento solicitado, ou `failed` em caso de falha. Erros de preparação ficam em `error`, associados a `run_id`. CLI e MCP iniciam em `starting`, com estado novo antes da preparação, sem reutilizar tarefas ou detalhes da execução anterior. O estado passa a `running` quando a preparação termina e as tarefas começam; `state`, `run_id` e `recovery_required` estão presentes em todas as gravações; durante o processamento consulte os estados individuais.

Os estados das tarefas são `pending`, `running`, `review`, `approved`, `failed`, `blocked` e `cancelled`. Uma falha bloqueia todos os descendentes. `approved` significa revisão aprovada, testes do worktree aprovados, cherry-pick aplicado e testes de integração aprovados.

Em `details.<id>`, `commit` identifica o candidato do worktree e `target_before` identifica o HEAD de destino antes do cherry-pick. O worker salva `integrated_commit` assim que identifica o commit aplicado, antes dos testes. Se a integração exigir um revert, `revert_commit` identifica o commit que restaurou a árvore. Execuções de versões anteriores podem conter apenas `commit`.

Se a gravação da aprovação falhar, a execução para e mantém a reserva. Quando o armazenamento volta a aceitar a escrita do diagnóstico, o status registra a tarefa aprovada, o SHA integrado e a necessidade de recuperação manual. Se o erro persistir, consulte o último status gravado e confira `integrated_commit` no histórico da branch antes de reenviar a tarefa.

Worktrees ficam em `.agent-runs/runs/<identificador-da-execução>/wt-<id>`. Cada execução tem seu próprio diretório e branches únicas; IDs de tarefas podem ser reutilizados. Os worktrees são preservados para inspeção. O mesmo diretório guarda `status.json`, `plan.json`, `run.log` e `pid.txt` (os dois últimos apenas em execuções iniciadas pelo MCP), o marcador `claim` do worker que assumiu a execução e, quando houver pedido de encerramento, o arquivo `stop`. `.agent-runs/latest-run` aponta para a execução mais recente. CLI e MCP gravam `plan.json` de forma atômica, sob a reserva de execução, antes da preparação dos workers. `get_submitted_plan()` retorna esse plano, inclusive se a preparação falhar. Uma execução recusada por reserva ativa não substitui o plano existente. Os registros de execuções anteriores permanecem em seus diretórios e podem ser removidos manualmente depois de inspecionados.

No Windows, o runner cria cada comando suspenso, associa-o a um Job Object exclusivo e só então libera sua execução. Ao terminar, inclusive em timeout ou cancelamento, o runner encerra os descendentes e confirma que o Job ficou vazio. Se a associação falhar, o comando é encerrado ainda suspenso. O módulo `windows_job.py` deve acompanhar `orchestrator.py` e usa a dependência `pywin32`.

Em POSIX, o runner cria um grupo de processos por comando. Ao término, envia `SIGKILL` aos membros restantes do grupo e aguarda seu encerramento. Um processo que criar outra sessão ou sair do grupo escapa desse controle. Se o encerramento do grupo ou do Job ficar incerto, o worker bloqueia a integração e mantém a reserva para inspeção manual.

Durante a implementação paralela, `Ctrl+C` aguarda os workers e verifica seus resultados antes de liberar a reserva. Nenhuma tarefa desse lote integra após a interrupção. Se algum worker exigir recuperação manual, ou se a espera for novamente interrompida sem confirmar o término, a reserva permanece ativa com `recovery_required: true`.

Interrupções durante o encerramento por timeout também preservam a reserva, inclusive se a tentativa adicional de encerrar o processo falhar. No Windows, uma interrupção após criar o subprocesso encerra também a raiz ainda suspensa; se a criação for interrompida antes de retornar seu identificador, a reserva é mantida por precaução. Em falhas de encerramento, o Job Object é fechado antes dos pipes, para que descendentes ativos não bloqueiem a limpeza. Se houver erro ao submeter ou coletar workers, os workers já iniciados são aguardados e inspecionados; recuperação pendente ou submissão incompleta mantém a reserva para inspeção manual.

Se houver timeout, exceção ou `Ctrl+C` nos testes após o cherry-pick, e o comando tiver preservado a branch, o commit e o Git limpo, o orquestrador tenta restaurar o código por um **novo commit de revert**, usando o hash do commit integrado. Isso preserva o histórico. Após um revert bem-sucedido, `Ctrl+C` encerra a execução com estado `failed`. Se os testes alterarem a branch, arquivos ou HEAD, o código é preservado para recuperação manual, sem revert automático. Se a leitura do HEAD integrado ou a verificação de branch/HEAD/arquivos falhar ou for interrompida, a reserva também é mantida, nenhuma outra tarefa integra e nenhum revert automático é tentado. Falha ou interrupção durante cherry-pick, revert ou abort que deixe o resultado Git incerto preserva a reserva e exige intervenção manual; consulte o erro e `git status` antes de continuar.

A branch e o commit de destino são conferidos antes de cada integração e no encerramento, inclusive quando a última tarefa falha antes da revisão. Se houver interrupção logo após aplicar o cherry-pick e antes de confirmar os testes de integração, o commit aplicado fica disponível para inspeção e a reserva permanece ativa. Uma troca de branch durante a implementação ou revisão também exige recuperação. Evite operações Git externas enquanto o orquestrador estiver ativo, pois outro processo pode alterar o repositório entre duas verificações.

Após encerramento abrupto:

1. Leia `run.log`, `status.json` e `pid.txt` (PID do worker MCP) em `.agent-runs/runs/<run_id>/`; o `run_id` está em `.agent-runs/run.lock`. Se a submissão avisou que não conseguiu gravar `pid.txt`, use o PID informado na resposta do MCP. Confirme no Gerenciador de Tarefas que o worker e seus subprocessos terminaram. A reserva também registra o PID do processo que a criou, que no MCP é o servidor.
2. Inspecione `git status`, `git log -5 --oneline` e `git worktree list`. Preserve alterações e resolva operações Git pendentes antes de começar outra execução. Não há retomada automática.
3. **Somente após confirmar que não há execução ativa**, remova a reserva com `Remove-Item -LiteralPath .agent-runs/run.lock`. Não remova uma reserva ativa.
4. Corrija a causa, confira a branch e o Git limpo, ajuste o plano se necessário e submeta novamente.

Para liberar um worktree antigo após inspecioná-lo, use `git worktree remove "CAMINHO_EXATO"`. O Git recusa worktrees sujos; preserve o trabalho antes de removê-los. As branches são mantidas e podem ser removidas manualmente quando não forem mais necessárias.

Diagnóstico rápido: erro `not a git repository` exige executar na raiz Git com commit inicial; `Instale/autentique` exige conferir o PATH do processo MCP; falhas antes de gerar estado aparecem no log do MCP. Reinicie a sessão do aplicativo após mudar PATH ou `.mcp.json`.

## Dependências e validação de alterações

`requirements.txt` fixa as dependências diretas; `requirements.lock.txt` fixa também as transitivas do ambiente validado, com `pywin32` restrito ao Windows. Use o lock na instalação e na CI. Atualizações devem ser intencionais: regenere as versões em ambiente limpo, execute `pip check`, a suíte completa e o smoke em cópia descartável. O workflow não chama modelos nem valida autenticação.

A autenticação no aplicativo Claude e a execução real com os modelos devem ser verificadas na máquina do operador. Retomada automática após crash e orçamento de tokens/custo continuam fora do escopo deste protótipo; a reserva e a recuperação manual continuam obrigatórias nesses casos.
