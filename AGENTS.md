# Convenções para agentes (`AGENTS.md`)

Regras para quem desenvolve o próprio Contracanto, seja uma pessoa, o Claude Code ou um executor do Contracanto orquestrando este repositório. O `CLAUDE.md` importa este arquivo. O Codex o lê sozinho.

Os guias em `contracanto/architect.md` e `contracanto/executor.md` não são regras deste repositório: são o texto que o Contracanto entrega ao arquiteto e ao executor dos projetos que ele orquestra. Mexa neles quando a tarefa for mudar esse comportamento.

## Verificação

Antes de encerrar uma tarefa ou preparar um commit, rode a mesma verificação da CI, nesta ordem, e só termine com tudo verde:

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe scripts/run_tests.py
```

- Use `ruff format` só nos arquivos que você alterou.
- Ao mudar dependências, atualize `pyproject.toml` e `requirements.lock.txt` juntos e rode `scripts/check_lock.py`.
- Os testes não chamam modelos nem consomem cota. Um teste novo também não pode chamar: simule `agents.run_agy`, `agents.run_codex` ou `process.run`.
- Execuções reais, como o smoke de `examples/` ou um plano contra outro repositório, consomem a cota dos planos do operador. Rode só quando ele pedir, e num repositório descartável numa pasta temporária.

## Código

- Python 3.13 ou mais novo, com tipos conferidos pelo `mypy`. O `ruff` define o estilo: aspas simples e linhas de até 120 caracteres.
- Identificadores em inglês. Comentários, docstrings, mensagens de erro e de log em português. Alguns arquivos de teste recentes têm docstrings em inglês: num arquivo existente, siga o idioma dele; num arquivo novo, use português.
- Fim de linha LF em tudo (`.gitattributes`). No Windows, `Path.write_text` grava CRLF: para gerar ou editar arquivos por script, use `write_bytes` com o texto em UTF-8.
- Cada pedido a um agente é uma constante de instruções seguida do JSON do payload na última linha (`agents._with_payload`). Os testes leem o payload com `rsplit('\n', 1)`. Mantenha esse formato.

## Invariantes

Não enfraqueça, nem para fazer um teste passar:

- o escopo por `files` (`gitops.check_scope`) e a recusa de arquivos alterados fora dele;
- a revisão obrigatória e os testes na base, no worktree e na integração;
- a reserva de execução (`run.lock`) e o estado `recovery_required`, que exige recuperação manual quando o Git ou os subprocessos ficam em estado incerto;
- o sandbox dos executores: `--mode auto --sandbox` no `agy`, e `workspace-write` sem rede e sem a configuração do usuário no Codex;
- o comportamento de falhar fechado diante de saída que não se entende, de um agente ou do Git.

## Segredos e caminhos

- Não leia, imprima nem grave segredos, tokens ou credenciais, nem a configuração e o login do Codex, do `agy` ou do Claude.
- Não ponha caminhos locais da máquina, como caminhos absolutos de usuário, em arquivos versionados: código, testes, fixtures, docs. As fixtures de saída real dos CLIs ficam sanitizadas (`<WORKTREE>`, `<HOME>`).

## Documentação

Toda a documentação é em português. Atualize no mesmo commit da mudança:

- `README.md`: variáveis `CONTRACANTO_*` (tabela), ferramentas MCP, comandos da linha de comando e a tabela de módulos em "Estrutura do código";
- `docs/recuperacao.md`: campos do `status.json`, categorias de falha e a tabela de solução de problemas;
- `contracanto/architect.md`, ao criar ou mudar uma categoria de falha em `contracanto/errors.py`. Um teste confere que toda categoria aparece no guia;
- `docs/plano-custos-e-executor.md`, só quando a tarefa for sobre ele.

## Commits

- Use prefixo convencional (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `build:`, `chore:`) e assunto em português.
- Separe mudanças estruturais (mover, renomear, formatar) de mudanças de comportamento, que vão com seus testes.
- Não faça commits intermediários com a verificação quebrada.
- Não faça push nem crie tags sem pedido explícito do operador.
