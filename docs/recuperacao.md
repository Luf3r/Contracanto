# Estado e recuperação

Referência do estado persistido de uma execução, do encerramento solicitado e do procedimento manual após uma falha. Visão geral e instalação estão no [README](../README.md).

Se você chegou aqui por causa de uma execução travada, vá direto para [Após um encerramento abrupto](#após-um-encerramento-abrupto).

## Acompanhar uma execução

`wait_for_run(timeout_seconds)` espera a execução terminar e devolve um resumo. `get_run_status()` e `contracanto --status` devolvem o relatório completo.

O relatório completo é um objeto JSON com quatro campos:

- `run`: o conteúdo de `status.json` da execução reservada ou, sem reserva, da mais recente; `null` se ela ainda não publicou estado.
- `reservation_state`: `active`, `none` ou `incomplete` (reserva em criação).
- `reservation`: `run_id`, `pid`, `stop_requested` e `worker_alive` da reserva ativa, ou `null`.
- `log`: as últimas linhas do log do worker lançado pelo MCP, com os eventos da execução (lotes, estados das tarefas, fases, vereditos, commits aplicados e erros).

`worker_alive` é `false` quando o processo que deveria estar trabalhando na execução não existe mais: o worker que a assumiu ou, antes disso, o worker lançado pelo MCP ou o processo que criou a reserva. Nesse caso o estado gravado não muda mais, mesmo que diga `running`. `wait_for_run` tenta então a retomada automática, descrita abaixo; se ela não for possível, siga [Após um encerramento abrupto](#após-um-encerramento-abrupto).

O resumo de `wait_for_run` traz `run_id`, `state`, `statuses`, `target`, `task_errors` (o começo do erro de cada tarefa que falhou), `recovery_required`, `error`, `duration_seconds`, `reservation_state`, `worker_alive`, `stop_requested` e `timed_out`. A ferramenta retorna assim que a execução chega a um estado final, o worker morre ou não há execução reservada. Se o tempo pedido (1 a 600 segundos) acabar antes, `timed_out` vem `true` e basta chamar de novo. Pareceres, tempos por fase e log ficam só no relatório completo.

### Estados

O estado global final é:

- `completed`, quando todas as tarefas são aprovadas;
- `cancelled`, quando o worker confirma o encerramento solicitado;
- `failed`, em qualquer outra falha.

CLI e MCP iniciam em `starting`, com estado novo, sem reutilizar tarefas ou detalhes da execução anterior. O estado passa a `running` quando a preparação termina e as tarefas começam. Erros de preparação ficam em `error`. `state`, `run_id` e `recovery_required` estão presentes em todas as gravações.

Os estados das tarefas são `pending`, `running`, `review`, `approved`, `failed`, `blocked` e `cancelled`. Uma falha bloqueia todos os descendentes. `approved` significa revisão aprovada, testes do worktree aprovados, cherry-pick aplicado e testes de integração aprovados.

### Tempos

Além de `state`, `run_id`, `statuses`, `details`, `recovery_required` e `error`, o `status.json` registra:

- `started_at`, `updated_at` e `finished_at`: horários em ISO 8601, UTC; `finished_at` é `null` até o estado final.
- `duration_seconds`: duração da execução até a última gravação ou até o estado final.
- `target`: a `branch` de destino (ref completa) e o commit `base` sobre o qual a execução começou; ausente quando a preparação falha antes disso. `branch_removed: true` indica que a branch de integração terminou vazia e foi apagada.
- `timings.<id>`: `started_at` e `finished_at` da tarefa e, em `phases`, os segundos acumulados e o número de vezes de cada fase (`implementation`, `review`, `repair`, `integration`). Uma tarefa bloqueada antes de começar não tem `started_at`. Quando o CLI chamado na fase informa tokens, a fase também traz `tokens`, descrito abaixo.
- `usage`: os tokens somados da execução por papel, em `executor` (implementação e correções) e `reviewer` (revisões). O campo só aparece depois da primeira chamada medida.

`tokens` e cada papel de `usage` têm o mesmo formato, com os campos somados entre todas as chamadas, inclusive as que falharam e as novas tentativas:

- `input`: entrada sem cache;
- `cached_input`: entrada lida do cache;
- `cache_write`: entrada gravada no cache;
- `output`: tudo o que o modelo gerou, inclusive o raciocínio;
- `reasoning`: a parte de `output` gasta em raciocínio;
- `calls`: número de chamadas medidas.

Os números vêm do próprio CLI: do evento `result` do `agy` e do campo `usage` do `claude -p`. Uma saída sem contagem não registra nada e não faz a chamada falhar, então uma fase sem `tokens` não significa zero tokens.

### Detalhes de cada tarefa

Em `details.<id>`:

- `commit` identifica o candidato do worktree.
- `target_before` identifica o HEAD de destino antes do cherry-pick.
- `integrated_commit` é gravado assim que o worker identifica o commit aplicado, antes dos testes.
- `revert_commit` identifica o commit que restaurou a árvore, quando a integração exigiu um revert.
- `error` resume a falha e `review` guarda o último parecer.
- `reviews` lista cada parecer, com `attempt`, `approved`, `at` e `feedback`, gravado assim que o revisor responde, antes da correção; o motivo de uma rejeição também vai para o `run.log` e aparece no `--watch` enquanto a tarefa não termina.
- `warning` avisa quando a saída do teste de integração cita caminhos em `.agent-runs/`.
- `worktree_removed` ou `cleanup_error` registram a limpeza do worktree de uma tarefa aprovada.

O commit integrado leva no assunto o identificador e a descrição da tarefa, e no corpo a execução de origem.

## Encerrar uma execução

Pelo MCP, chame `stop_run()`. Você também pode passar o `run_id` obtido no status para selecionar uma execução específica. Pelo terminal, execute `contracanto --stop` (com o mesmo `--repo`, se usado) em outra janela. As duas formas servem para planos iniciados pela CLI ou pelo MCP.

O pedido retorna imediatamente. No status, `reservation.stop_requested` fica `true` enquanto o worker encerra os comandos de modelo e de testes. O que acontece em seguida:

- Operações Git em andamento chegam a um ponto seguro.
- Se o cancelamento interromper os testes de integração, o worker verifica o repositório e reverte o commit em teste quando for seguro.
- Tarefas já aprovadas mantêm seus commits.
- Se o plano já tiver terminado, vale o resultado registrado.
- Um encerramento incerto continua exigindo recuperação manual.

Confirme o estado final `cancelled` no status ou em `.agent-runs/runs/<run_id>/status.json`.

## A reserva

CLI e MCP compartilham uma reserva exclusiva em `.agent-runs/run.lock`, criada antes de gravar o plano ou iniciar o processo. Uma segunda execução é recusada.

A reserva é liberada ao término normal e em erros tratados com estado conhecido. Ela é mantida, com `recovery_required: true`, em falhas de recuperação Git e em encerramentos incertos de subprocessos, para impedir novas execuções até a inspeção manual. Interrupções abruptas também podem preservá-la.

O status expõe só o identificador público da execução. O worker usa uma credencial de uso único para assumir a reserva; repetir a chamada do worker preserva o plano, o estado e a reserva da execução ativa.

`submit_plan` responde com um texto iniciado por `Erro:` quando a entrada é inválida ou já existe execução reservada, sem iniciar nada. Falhas ao lançar o worker são reportadas como erro da ferramenta. Se não conseguir gravar `pid.txt` depois de iniciar o worker, `submit_plan` confirma o início e retorna um aviso com o PID correto; a execução continua, e nesse caso `pid.txt` pode estar ausente ou incompleto.

## Arquivos de uma execução

Tudo fica em `.agent-runs/runs/<run_id>/`:

| Arquivo | Conteúdo |
| --- | --- |
| `status.json` | Estado da execução, substituído de forma atômica a cada atualização |
| `plan.json` | Plano recebido, gravado sob a reserva antes da preparação |
| `options.json` | Opções da execução (paralelismo, tentativas, comandos, branch), para repeti-la em uma retomada |
| `run.log` | Eventos e saída do worker (só em execuções iniciadas pelo MCP) |
| `pid.txt` | PID do worker lançado pelo MCP |
| `claim/pid` | PID do worker que assumiu a execução, pela CLI ou pelo MCP |
| `commands/` | Um arquivo por comando demorado em andamento (`agy`, revisor, testes ou preparação), com o PID do processo raiz no nome |
| `stop` | Presente quando houve pedido de encerramento |
| `wt-<id>/` | Worktree de cada tarefa, só em execuções antigas; as novas usam a pasta abaixo |

Os worktrees ficam fora do repositório, em `<pasta de worktrees>/<repositório>-<hash>/<16 primeiros caracteres do run_id>/wt-<id>/`. A pasta é `CONTRACANTO_WORKTREES_DIR` ou, sem ela, `%LOCALAPPDATA%\Contracanto\worktrees` no Windows e `~/.cache/Contracanto/worktrees` nos demais sistemas. O caminho exato de cada um está em `details.<id>.worktree` do status e em `git worktree list`.

`.agent-runs/latest-run` aponta para a execução mais recente. `get_submitted_plan()` retorna o `plan.json`, inclusive se a preparação falhar; uma execução recusada por reserva ativa não substitui o plano existente.

Cada execução tem seu próprio diretório e branches únicas, então IDs de tarefas podem ser reutilizados. Worktrees de tarefas aprovadas são removidos após a integração, salvo com `--keep-worktrees`; os demais são preservados para inspeção. Os registros de execuções anteriores permanecem até serem removidos com `--prune-runs`.

Detalhes da gravação:

- Se uma consulta no Windows bloquear a substituição do JSON ou a remoção de `run.lock`, o processo tenta novamente por até um segundo. Antes de cada tentativa de remoção, ele confere a identidade da reserva.
- Se a primeira gravação de `starting` falhar, o diagnóstico da nova execução usa listas de tarefas e detalhes vazias.
- Uma recuperação já exigida mantém a reserva mesmo se houver erro ao salvar o estado final ou ao ler o estado da execução em curso.
- Se a gravação da aprovação falhar, a execução para e mantém a reserva. Quando o armazenamento volta a aceitar a escrita, o status registra a tarefa aprovada, o SHA integrado e a necessidade de recuperação manual. Se o erro persistir, confira `integrated_commit` no histórico da branch antes de reenviar a tarefa.

## Garantias por etapa

### Subprocessos

No Windows, o runner cria cada comando suspenso, associa-o a um Job Object exclusivo e só então libera sua execução. Ao terminar, inclusive em timeout ou cancelamento, o runner encerra os descendentes e confirma que o Job ficou vazio. Se a associação falhar, o comando é encerrado ainda suspenso. Como o Job Object é fechado junto com o worker, encerrar o worker à força também encerra os comandos que ele iniciou. O módulo `contracanto/windows_job.py` usa a dependência `pywin32`.

Em POSIX, o runner cria um grupo de processos por comando. Ao término, envia `SIGKILL` aos membros restantes do grupo e aguarda seu encerramento. Um processo que criar outra sessão ou sair do grupo escapa desse controle. Se o próprio worker for encerrado à força, os grupos que ele criou continuam rodando; os registros em `commands/` permitem que `--unlock` os encontre e recuse a liberação enquanto existirem.

Se o encerramento do grupo ou do Job ficar incerto, o worker bloqueia a integração e mantém a reserva para inspeção manual. O mesmo vale para interrupções durante o encerramento por timeout, inclusive se a tentativa adicional de encerrar o processo falhar. No Windows, uma interrupção após criar o subprocesso encerra também a raiz ainda suspensa; se a criação for interrompida antes de retornar seu identificador, a reserva é mantida por precaução. Em falhas de encerramento, o Job Object é fechado antes dos pipes, para que descendentes ativos não bloqueiem a limpeza.

### Implementação e revisão em paralelo

`Ctrl+C` durante um lote aguarda os workers e verifica seus resultados antes de liberar a reserva. Nenhuma tarefa desse lote integra após a interrupção. Se algum worker exigir recuperação manual, ou se a espera for novamente interrompida sem confirmar o término, a reserva permanece ativa com `recovery_required: true`. Se houver erro ao submeter ou coletar workers, os já iniciados são aguardados e inspecionados; recuperação pendente ou submissão incompleta mantém a reserva.

Uma chamada ao `agy` que falha, na implementação ou em uma correção, é repetida no mesmo worktree até `CONTRACANTO_AGENT_RETRIES` vezes, desde que a branch e o commit do worktree estejam intactos. Um encerramento incerto nunca é repetido.

### Integração

A branch e o commit de destino são conferidos antes de cada integração e no encerramento, inclusive quando a última tarefa falha antes da revisão. Uma troca de branch durante a implementação ou a revisão também exige recuperação. Evite operações Git externas enquanto o orquestrador estiver ativo, pois outro processo pode alterar o repositório entre duas verificações.

Se houver timeout, exceção ou `Ctrl+C` nos testes após o cherry-pick, e o comando tiver preservado a branch, o commit e o Git limpo, o orquestrador restaura o código com um **novo commit de revert**, usando o hash do commit integrado. Isso preserva o histórico. Após um revert bem-sucedido, `Ctrl+C` encerra a execução com estado `failed`.

A reserva é mantida, sem revert automático, quando:

- os testes alteram a branch, arquivos ou o HEAD;
- a leitura do HEAD integrado ou a verificação de branch, HEAD e arquivos falha ou é interrompida;
- uma falha ou interrupção durante cherry-pick, revert ou abort deixa o resultado Git incerto;
- há interrupção logo após aplicar o cherry-pick e antes de confirmar os testes de integração. O commit aplicado fica disponível para inspeção.

Nesses casos nenhuma outra tarefa integra. Consulte o erro e `git status` antes de continuar.

## Retomada automática

Quando o worker morre sem registrar um estado final, `wait_for_run` faz sozinho o que o procedimento manual abaixo descreve, se for seguro:

1. confere que o worker e os comandos que ele iniciou não estão mais rodando, que não há operação Git pendente e que o repositório está limpo (as conferências de `--unlock`);
2. libera a reserva e marca a execução interrompida como `failed`;
3. confere que os commits das tarefas aprovadas estão no HEAD e que nenhuma tarefa não aprovada deixou commit na branch (as conferências de `--resume`);
4. lança uma execução nova com as tarefas que faltavam e as opções gravadas em `options.json`, e continua esperando por ela.

O resumo traz então `auto_resume.resumed_from` e `auto_resume.attempt`. Se alguma conferência falhar, nada é forçado: `auto_resume.error` diz o motivo e vale o procedimento manual. `CONTRACANTO_AUTO_RESUME` limita quantas vezes uma mesma execução pode ser relançada em sequência (padrão 1; 0 desliga). A retomada acontece quando alguém chama `wait_for_run`; não há processo vigiando em segundo plano. Uma execução encerrada com `recovery_required: true` nunca é retomada automaticamente.

Pela CLI, `contracanto --resume --test ...` faz os passos 1 a 3 e executa as tarefas que faltavam.

## Após um encerramento abrupto

Use este procedimento quando a retomada automática não foi possível ou a execução pediu recuperação manual.


1. **Veja o que aconteceu.** Leia `run.log`, `status.json` e `pid.txt` em `.agent-runs/runs/<run_id>/`; o `run_id` está em `.agent-runs/run.lock`. Se a submissão avisou que não conseguiu gravar `pid.txt`, use o PID informado na resposta do MCP. A reserva também registra o PID do processo que a criou, que no MCP é o servidor.
2. **Confirme que nada mais está rodando.** No Gerenciador de Tarefas (ou com `ps`), confira que o worker e seus subprocessos terminaram.
3. **Inspecione o Git.** Use `git status`, `git log -5 --oneline` e `git worktree list`. Preserve alterações e resolva operações Git pendentes antes de começar outra execução.
4. **Libere a reserva** com `contracanto --unlock`.
5. **Corrija a causa**, confira a branch e o Git limpo e então submeta um plano novo ou refaça só o que faltou com `contracanto --resume --test ...` (ou `resume_run` pelo MCP).

A retomada cria uma execução nova com as tarefas que não estavam `approved`. Se o worker morreu entre o cherry-pick e a aprovação de uma tarefa, o commit dela está na branch com a tarefa marcada como `blocked`. A retomada detecta isso por `details.<id>.integrated_commit` e recusa, com o commit no erro: desfaça-o com `git revert`, que ela reconhece pela mensagem padrão do revert, ou submeta um plano novo sem a tarefa.

O `--unlock` recusa, com código de saída 1, em qualquer destes casos:

- o worker da execução ainda está vivo, pelo PID gravado em `runs/<run_id>/claim/pid` ao assumir a execução ou pelo `pid.txt` do MCP;
- algum comando registrado em `runs/<run_id>/commands/` continua rodando;
- há operação Git pendente;
- o repositório tem alterações.

Ao liberar, ele marca a execução como `failed` se ela não tinha estado final, passa para `blocked` as tarefas que ainda constavam em andamento e registra `unlocked_at`. Um PID reaproveitado por outro processo também causa recusa; confirme no Gerenciador de Tarefas e, só então, remova `.agent-runs/run.lock` manualmente.

## Limpar execuções antigas

`contracanto --prune-runs N` remove os registros das execuções encerradas, com seus worktrees e branches `agent/*`, mantendo as N mais recentes. Nunca remove a execução reservada, uma sem estado final ou uma com recuperação pendente.

Uma execução cujo worktree tem alterações não salvas, como costuma acontecer quando a tarefa falha, é mantida. Depois de inspecioná-la, acrescente `--force` para descartar essas alterações.

Para liberar um único worktree, use `git worktree remove "CAMINHO_EXATO"`. O Git recusa worktrees sujos, e nesse caminho manual a branch `agent/*` permanece até um `git branch -D`.

## Diagnóstico rápido

| Sintoma | O que conferir |
| --- | --- |
| `not a git repository` | Execute na raiz Git, com um commit inicial |
| `é um subdiretório do repositório Git` | Informe em `--repo` ou `CONTRACANTO_REPO` a raiz do repositório |
| `HEAD destacado` | Faça checkout de uma branch ou informe `--branch NOME` |
| `O operador fixou o comando` | Use o comando mostrado no erro, definido em `CONTRACANTO_TEST_COMMAND` ou `CONTRACANTO_SETUP_COMMAND` |
| `Instale/autentique agy e claude` | O PATH do processo MCP |
| `Comando de testes não encontrado` ou `Comando de preparação não encontrado` | O nome do executável e o PATH do worker |
| Nenhum estado gerado | O log do MCP; falhas antes do primeiro estado aparecem ali |

Reinicie a sessão do aplicativo depois de mudar o PATH ou o `.mcp.json`.
