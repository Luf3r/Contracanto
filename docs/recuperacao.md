# Estado e recuperação

Referência do estado persistido de uma execução, do encerramento solicitado e do procedimento manual após uma falha. Visão geral e instalação estão no [README](../README.md).

Para encerrar o plano ativo pelo MCP, chame `stop_run()`. Você também pode passar o `run_id` obtido em `get_run_status()` para selecionar uma execução específica. Pelo terminal, execute `py orchestrator.py --stop` (com o mesmo `--repo`, se usado) na raiz do projeto, em outra janela. As duas opções servem para planos iniciados pela CLI ou pelo MCP.

O pedido retorna imediatamente. Em `get_run_status()`, `reservation.stop_requested` fica `true` enquanto o worker encerra os comandos de modelo e testes. Operações Git em andamento chegam a um ponto seguro. Se o cancelamento interromper os testes de integração, o worker verifica o repositório e reverte o commit em teste quando for seguro. Tarefas já aprovadas mantêm seus commits. Consulte `get_run_status()` ou `.agent-runs/runs/<run_id>/status.json` para confirmar o estado final `cancelled`. Se o plano já tiver terminado, vale o resultado registrado. Um encerramento incerto continua exigindo recuperação manual.

CLI e MCP compartilham uma reserva exclusiva em `.agent-runs/run.lock`, criada antes de gravar o plano ou iniciar o processo. Uma segunda execução é recusada. A reserva é liberada ao término normal e em erros tratados com estado conhecido. Em falhas de recuperação Git ou encerramento incerto de subprocessos, `recovery_required: true` preserva a reserva para impedir novas execuções até inspeção manual. Interrupções abruptas também podem preservá-la.

O status expõe o identificador público da execução. O worker usa uma credencial de uso único para assumir a reserva. Repetir a chamada do worker preserva o plano, o estado e a reserva da execução ativa.

`get_run_status()` retorna um objeto JSON com quatro campos:

- `run`: o conteúdo de `status.json` da execução reservada ou, sem reserva, da mais recente; `null` se ela ainda não publicou estado.
- `reservation_state`: `active`, `none` ou `incomplete` (reserva em criação).
- `reservation`: `run_id`, `pid` e `stop_requested` da reserva ativa, ou `null`.
- `log`: as últimas linhas do log do worker lançado pelo MCP.

Além de `state`, `run_id`, `statuses`, `details`, `recovery_required` e `error`, o `status.json` registra o tempo:

- `started_at`, `updated_at` e `finished_at`: horários em ISO 8601, UTC; `finished_at` é `null` até o estado final.
- `duration_seconds`: duração da execução até a última gravação ou até o estado final.
- `timings.<id>`: `started_at` e `finished_at` da tarefa e, em `phases`, os segundos acumulados e o número de vezes de cada fase (`implementation`, `review`, `repair`, `integration`). Uma tarefa bloqueada antes de começar não tem `started_at`.

`py orchestrator.py --status` imprime o mesmo objeto que `get_run_status()`.

Se a reserva for removida durante a consulta, `reservation_state` volta a `none`. `submit_plan` responde com um texto iniciado por `Erro:` quando a entrada é inválida ou já existe execução reservada, sem iniciar nada; falhas ao lançar o worker são reportadas como erro da ferramenta. Se não conseguir gravar `pid.txt` após iniciar o worker, `submit_plan` confirma o início e retorna um aviso com o PID correto; a execução continua e a reserva permanece sob controle do worker. Nesse caso, `pid.txt` pode estar ausente ou incompleto: use o PID da resposta para inspeção.

As atualizações de estado substituem o JSON de forma atômica. Se uma consulta no Windows bloquear a substituição do JSON ou a remoção de `run.lock`, o processo tenta novamente por até um segundo. Antes de cada tentativa de remoção, ele confere a identidade da reserva. Se a primeira gravação de `starting` falhar, o diagnóstico da nova execução usa listas de tarefas e detalhes vazias. Uma recuperação já exigida mantém a reserva mesmo se houver erro ao salvar o estado final ou ao ler o estado da execução em curso.

O estado global final é `completed` quando todas as tarefas são aprovadas, `cancelled` quando o worker confirma o encerramento solicitado, ou `failed` em caso de falha. Erros de preparação ficam em `error`, associados a `run_id`. CLI e MCP iniciam em `starting`, com estado novo antes da preparação, sem reutilizar tarefas ou detalhes da execução anterior. O estado passa a `running` quando a preparação termina e as tarefas começam; `state`, `run_id` e `recovery_required` estão presentes em todas as gravações; durante o processamento consulte os estados individuais.

Os estados das tarefas são `pending`, `running`, `review`, `approved`, `failed`, `blocked` e `cancelled`. Uma falha bloqueia todos os descendentes. `approved` significa revisão aprovada, testes do worktree aprovados, cherry-pick aplicado e testes de integração aprovados.

Em `details.<id>`, `commit` identifica o candidato do worktree e `target_before` identifica o HEAD de destino antes do cherry-pick. O worker salva `integrated_commit` assim que identifica o commit aplicado, antes dos testes. Se a integração exigir um revert, `revert_commit` identifica o commit que restaurou a árvore. Execuções de versões anteriores podem conter apenas `commit`.

Se a gravação da aprovação falhar, a execução para e mantém a reserva. Quando o armazenamento volta a aceitar a escrita do diagnóstico, o status registra a tarefa aprovada, o SHA integrado e a necessidade de recuperação manual. Se o erro persistir, consulte o último status gravado e confira `integrated_commit` no histórico da branch antes de reenviar a tarefa.

Worktrees ficam em `.agent-runs/runs/<identificador-da-execução>/wt-<id>`. Cada execução tem seu próprio diretório e branches únicas; IDs de tarefas podem ser reutilizados. Worktrees de tarefas aprovadas são removidos após a integração, salvo com `--keep-worktrees`; os demais são preservados para inspeção. O mesmo diretório guarda `status.json`, `plan.json`, `run.log` e `pid.txt` (os dois últimos apenas em execuções iniciadas pelo MCP), o marcador `claim` do worker que assumiu a execução e, quando houver pedido de encerramento, o arquivo `stop`. `.agent-runs/latest-run` aponta para a execução mais recente. CLI e MCP gravam `plan.json` de forma atômica, sob a reserva de execução, antes da preparação dos workers. `get_submitted_plan()` retorna esse plano, inclusive se a preparação falhar. Uma execução recusada por reserva ativa não substitui o plano existente. Os registros de execuções anteriores permanecem em seus diretórios e podem ser removidos manualmente depois de inspecionados.

No Windows, o runner cria cada comando suspenso, associa-o a um Job Object exclusivo e só então libera sua execução. Ao terminar, inclusive em timeout ou cancelamento, o runner encerra os descendentes e confirma que o Job ficou vazio. Se a associação falhar, o comando é encerrado ainda suspenso. O módulo `windows_job.py` deve acompanhar `orchestrator.py` e usa a dependência `pywin32`.

Em POSIX, o runner cria um grupo de processos por comando. Ao término, envia `SIGKILL` aos membros restantes do grupo e aguarda seu encerramento. Um processo que criar outra sessão ou sair do grupo escapa desse controle. Se o encerramento do grupo ou do Job ficar incerto, o worker bloqueia a integração e mantém a reserva para inspeção manual.

Durante a implementação paralela, `Ctrl+C` aguarda os workers e verifica seus resultados antes de liberar a reserva. Nenhuma tarefa desse lote integra após a interrupção. Se algum worker exigir recuperação manual, ou se a espera for novamente interrompida sem confirmar o término, a reserva permanece ativa com `recovery_required: true`.

Interrupções durante o encerramento por timeout também preservam a reserva, inclusive se a tentativa adicional de encerrar o processo falhar. No Windows, uma interrupção após criar o subprocesso encerra também a raiz ainda suspensa; se a criação for interrompida antes de retornar seu identificador, a reserva é mantida por precaução. Em falhas de encerramento, o Job Object é fechado antes dos pipes, para que descendentes ativos não bloqueiem a limpeza. Se houver erro ao submeter ou coletar workers, os workers já iniciados são aguardados e inspecionados; recuperação pendente ou submissão incompleta mantém a reserva para inspeção manual.

Se houver timeout, exceção ou `Ctrl+C` nos testes após o cherry-pick, e o comando tiver preservado a branch, o commit e o Git limpo, o orquestrador tenta restaurar o código por um **novo commit de revert**, usando o hash do commit integrado. Isso preserva o histórico. Após um revert bem-sucedido, `Ctrl+C` encerra a execução com estado `failed`. Se os testes alterarem a branch, arquivos ou HEAD, o código é preservado para recuperação manual, sem revert automático. Se a leitura do HEAD integrado ou a verificação de branch/HEAD/arquivos falhar ou for interrompida, a reserva também é mantida, nenhuma outra tarefa integra e nenhum revert automático é tentado. Falha ou interrupção durante cherry-pick, revert ou abort que deixe o resultado Git incerto preserva a reserva e exige intervenção manual; consulte o erro e `git status` antes de continuar.

A branch e o commit de destino são conferidos antes de cada integração e no encerramento, inclusive quando a última tarefa falha antes da revisão. Se houver interrupção logo após aplicar o cherry-pick e antes de confirmar os testes de integração, o commit aplicado fica disponível para inspeção e a reserva permanece ativa. Uma troca de branch durante a implementação ou revisão também exige recuperação. Evite operações Git externas enquanto o orquestrador estiver ativo, pois outro processo pode alterar o repositório entre duas verificações.

Após encerramento abrupto:

1. Leia `run.log`, `status.json` e `pid.txt` (PID do worker MCP) em `.agent-runs/runs/<run_id>/`; o `run_id` está em `.agent-runs/run.lock`. Se a submissão avisou que não conseguiu gravar `pid.txt`, use o PID informado na resposta do MCP. Confirme no Gerenciador de Tarefas que o worker e seus subprocessos terminaram. A reserva também registra o PID do processo que a criou, que no MCP é o servidor.
2. Inspecione `git status`, `git log -5 --oneline` e `git worktree list`. Preserve alterações e resolva operações Git pendentes antes de começar outra execução. Não há retomada automática.
3. Libere a reserva com `py orchestrator.py --unlock`. O comando confere se o worker da execução ainda está vivo (pelo PID que ele grava em `runs/<run_id>/claim/pid` ao assumir a execução, ou pelo `pid.txt` do MCP), se há operação Git pendente e se o repositório tem alterações, e recusa em qualquer desses casos. Ao liberar, marca a execução como `failed` se ela não tinha estado final e registra `unlocked_at`. Um PID reaproveitado por outro processo também causa recusa; confirme no Gerenciador de Tarefas e, só então, remova `.agent-runs/run.lock` manualmente.
4. Corrija a causa, confira a branch e o Git limpo, ajuste o plano se necessário e submeta novamente.

Para remover os registros de execuções antigas, com seus worktrees e branches `agent/*`, use `py orchestrator.py --prune-runs N`, que mantém as N mais recentes. Para liberar um único worktree após inspecioná-lo, use `git worktree remove "CAMINHO_EXATO"`. O Git recusa worktrees sujos; preserve o trabalho antes de removê-los. As branches `agent/*` desses worktrees são mantidas e podem ser removidas manualmente com `git branch -D` quando não forem mais necessárias.

Diagnóstico rápido: erro `not a git repository` exige executar na raiz Git com commit inicial; `Instale/autentique` exige conferir o PATH do processo MCP; falhas antes de gerar estado aparecem no log do MCP. Reinicie a sessão do aplicativo após mudar PATH ou `.mcp.json`.
