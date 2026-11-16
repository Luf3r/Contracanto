"""Verificação do plano smoke; chamada explicitamente após sua implementação."""

from pathlib import Path

if __name__ == '__main__':
    content = Path('smoke.txt').read_text(encoding='utf-8')
    if content != 'orchestrator-ok\n':
        raise SystemExit('Conteúdo inesperado em smoke.txt')
    print('Smoke OK')
