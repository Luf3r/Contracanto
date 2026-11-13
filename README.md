# Claude Desktop → MCP → Python → Antigravity CLI

**Protótipo de integração local para Windows**, não validado contra suas credenciais, CLI e repositório reais. Não é solução de produção.

## Pré-requisitos
- Windows, Python 3.11+, Git for Windows
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
1. Copie os arquivos deste pacote para **a raiz do repositório Git** que deseja modificar. O script supõe que `orchestrator.py` está na raiz do repositório. Confirme com `git status` que as únicas modificações são os novos arquivos e faça um commit inicial.
2. No PowerShell, dentro do projeto:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
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
4. Use **preferencialmente** o modo Auto com sandbox e allowlist limitada no Antigravity, jamais `--dangerously-skip-permissions`. O modo headless pode negar comandos não autorizados. Configure permissões granulares conforme docs: https://antigravity.google/docs/cli/headless/

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

Há regressões de parsing da CLI, validação de escopo, dependências, reserva de execução e rollback por timeout. Um teste usa commits, worktree, cherry-pick e revert reais em um repositório temporário dentro de `.agent-runs/`.

Para testar os CLIs reais, use uma cópia descartável deste projeto com Git limpo, autenticações ativas e permissões adequadas. O smoke cria apenas `smoke.txt` e **consome chamadas dos modelos**:

```powershell
.\.venv\Scripts\python.exe orchestrator.py plan.smoke.json --parallel 1 --retries 0 --test python smoke_check.py
```

O processo deve retornar código 0 e a task `smoke` deve terminar `approved`. O arquivo deve conter `orchestrator-ok` e uma quebra de linha. O comando `python` precisa apontar para um interpretador real no PATH dos workers; se necessário, substitua-o pelo caminho absoluto do Python do venv.

## Formato do plano

O plano é um objeto JSON com `tasks` (1 a 100 tarefas). `goal` é contexto descritivo opcional. Cada tarefa exige:

| Campo | Contrato |
| --- | --- |
| `id` | String única, 1 a 60 caracteres: letras ASCII, números, `_` ou `-` |
| `description` | String não vazia com a instrução de implementação |
| `deps` | Lista de IDs existentes; pode ser vazia; sem autorreferência ou ciclos |
| `files` | Lista não vazia de arquivos ou diretórios relativos, usando `/` |
| `acceptance` | Lista não vazia de strings com critérios verificáveis |

Diretórios autorizam seus descendentes. Caminhos absolutos, `..`, `.`, `\\`, componentes vazios, `.git` e `.agent-runs` são rejeitados. A comparação de ownership é textual; use a grafia exata e consistente dos caminhos, inclusive maiúsculas/minúsculas. A verificação de escopo é posterior à execução e não é uma sandbox.

No MCP, tanto o plano quanto o comando de teste são strings contendo JSON. Exemplo de argumentos de `submit_plan`:

```json
{
  "plan_json": "{\"tasks\":[{\"id\":\"smoke\",\"description\":\"Crie smoke.txt com orchestrator-ok e uma quebra de linha\",\"deps\":[],\"files\":[\"smoke.txt\"],\"acceptance\":[\"Conteúdo exato\"]}]}",
  "parallel": 1,
  "retries": 0,
  "test_command_json": "[\"python\",\"smoke_check.py\"]"
}
```

O comando é uma lista de argumentos executada sem shell. Operadores como `&&`, pipes e redirecionamentos não são interpretados. `parallel` aceita 1 a 8; `retries`, 0 a 3 e controla correções após rejeição da revisão, não falhas de infraestrutura.

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

Alternativamente, configure o MCP no ambiente local do Claude Code. O MCP de Chat e o da aba Code não são os mesmos. Reinicie a sessão local do Claude e verifique se aparecem as ferramentas `submit_plan`, `get_run_status` e `get_submitted_plan`.

Peça ao Claude na aba Code:

> Inspecione este repositório, proponha um plano em tarefas pequenas e verificáveis com dependências e arquivos autorizados; use submit_plan para iniciar execução com 3 workers e testes `py -m pytest`. Revise as tasks automaticamente via Claude CLI. Não faça deploy/push ou modificações destrutivas. Consulte get_run_status ao final.

## Comportamento e limitações
- O Claude cria o grafo de tarefas e envia pelo MCP. A fila é executada por Python, chamando `agy` em worktrees locais isolados.
- O Claude Code CLI revisa diffs usando explicitamente o alias `sonnet`. Uma aprovação **não** substitui os testes.
- Tarefas independentes, que não anunciam ownership conflitante, podem rodar em paralelo; a integração ocorre sequencialmente.
- Falhas de cherry-pick param a tarefa. Não faz deploy, push ou exclusões automáticas de worktrees.
- `get_run_status` precisa ser consultado; o aplicativo Claude não recebe notificação push de término.
- Não há retomada após desligar o processo, limitação de custos/tokens, isolamento forte de execução ou fila transacional. Não use em repositórios reais até adicionar esses recursos.
- Scripts usam revisão por modelo e não garantem correção ou segurança. Execute testes e auditoria adicionais antes de merge de produção.
- Manter `agy` e `claude` no PATH do processo MCP e ter autenticação ativa é obrigatório.

## Estado e recuperação

CLI e MCP compartilham uma reserva exclusiva em `.agent-runs/run.lock`, criada antes de gravar o plano ou iniciar o processo. Uma segunda execução é recusada. A reserva é liberada ao término normal, inclusive em erros tratados; interrupções abruptas podem preservá-la.

Os estados das tarefas são `pending`, `running`, `review`, `approved`, `failed` e `blocked`. Uma falha bloqueia todos os descendentes. `approved` significa revisão aprovada, testes do worktree aprovados, cherry-pick aplicado e testes de integração aprovados.

Worktrees ficam em `.agent-runs/runs/<identificador-da-execução>/wt-<id>`. Cada execução tem seu próprio diretório e branches únicas; IDs de tarefas podem ser reutilizados. Os worktrees são preservados para inspeção. `status.json` e o plano enviado mostram a execução mais recente; o log do MCP é sobrescrito a cada submissão.

Se houver timeout ou exceção nos testes após o cherry-pick, o orquestrador tenta restaurar o código por um **novo commit de revert**. Isso preserva o histórico. Falha do revert ou do abort de cherry-pick bloqueia todas as tarefas restantes e exige intervenção manual; consulte o erro e `git status` antes de continuar.

Após encerramento abrupto:

1. Leia `.agent-runs/run.log`, `status.json` e `pid.txt` (PID do worker MCP). Confirme no Gerenciador de Tarefas que o worker e seus subprocessos terminaram. A reserva também registra o PID do processo que a criou, que no MCP é o servidor.
2. Inspecione `git status`, `git log -5 --oneline` e `git worktree list`. Preserve alterações e resolva operações Git pendentes antes de começar outra execução. Não há retomada automática.
3. **Somente após confirmar que não há execução ativa**, remova a reserva com `Remove-Item -LiteralPath .agent-runs/run.lock`. Não remova uma reserva ativa.
4. Corrija a causa, confira a branch e o Git limpo, ajuste o plano se necessário e submeta novamente.

Para liberar um worktree antigo após inspecioná-lo, use `git worktree remove "CAMINHO_EXATO"`. O Git recusa worktrees sujos; preserve o trabalho antes de removê-los. As branches são mantidas e podem ser removidas manualmente quando não forem mais necessárias.

Diagnóstico rápido: erro `not a git repository` exige executar na raiz Git com commit inicial; `Instale/autentique` exige conferir o PATH do processo MCP; falhas antes de gerar estado aparecem no log do MCP. Reinicie a sessão do aplicativo após mudar PATH ou `.mcp.json`.
