# Testes - remote-link

Como o projeto é testado, o que cada camada garante e onde encaixar teste novo.
Guia de arquitetura: [CLAUDE.md](CLAUDE.md).

## Rodar tudo

```bash
./.venv/bin/python -m pytest --cov=server    # servidor  (gate: 100%)
npm run test:web                             # cliente   (vitest + jsdom)
npm run test:e2e                             # ponta a ponta (server real + Chromium)
```

Nada disso precisa de X11, GPU, `/dev/uinput` ou rede - exceto o `test:e2e`,
que sobe o `server.main` de verdade (o teste que captura tela se auto-pula sem
`DISPLAY`). Os testes forçam `RL_GUARDIAN=off` e `RL_ENCODER=x264`
(`tests/conftest.py`) pra não depender do hardware desta máquina.

Gates: `.coveragerc` exige **100%** de linha+branch em `server/`;
`vitest.config.js` exige 95% de linhas/funções/statements e 90% de branches em
`web/app.js`. Código novo entra com teste no mesmo commit.

## As camadas (e por que cada uma existe)

| camada | onde | responde a pergunta |
|---|---|---|
| unit | `tests/test_*.py`, `web/__tests__/app.test.js` | esta função faz o que promete? |
| integração | `tests/test_integration_ws.py` | as peças conversam pelo WebSocket de verdade? |
| propriedade/fuzz | `tests/test_fuzz_input.py`, `test_parsers_property.py`, `web/__tests__/protocol.test.js` | vale pra *qualquer* entrada, não só pro exemplo? |
| entrypoint | `tests/test_script.py` | o `./remote-link.sh` faz a coisa certa? |
| e2e | `web/e2e/*.spec.js` | funciona num browser real contra o servidor real? |
| anti-drift | `tests/test_docs_sync.py` | a documentação ainda descreve este código? |

### Servidor (`tests/`)

- **`test_main.py`** - `Hub`/`AudioHub`, filas, ciclo dos encoders, rotas.
- **`test_video.py`** - detecção de encoder, presets, `xrandr`, montagem do
  comando ffmpeg, fatiamento de AU, supervisão/retry.
- **`test_audio.py`** - presets Opus, `OggOpusParser`, fontes de áudio.
- **`test_inject.py`** - escrita evdev de mouse/teclado/gamepad.
- **`test_keymap.py`** - o mapa `KeyboardEvent.code` → código evdev.
- **`test_gamepad.py`** - persistência dos perfis de remap.
- **`test_guardian.py`** - escape hatch anti-ban (Linux e Windows).
- **`test_integration_ws.py`** - sobe o app aiohttp e fala pelos WS como o
  cliente fala: keyframe-gating, fan-out, `reset` na troca de preset, áudio
  arrastado (ou não) pelo preset de vídeo, troca de monitor, e a bateria de
  payloads tortos que **não** podem derrubar o canal de input.
- **`test_fuzz_input.py`** - fuzz determinístico do `Injector` e dos saneadores
  de qualidade. Invariante: o que chega no `/dev/uinput` fica **sempre** dentro
  da faixa declarada no device, e todo frame de gamepad fecha em `syn()`.
- **`test_parsers_property.py`** - os dois parsers incrementais (Ogg/Opus e
  Access Units H.264). Propriedade central: **o resultado não depende de como o
  stream foi picotado** - o mesmo stream é repartido de dezenas de formas
  aleatórias e a saída tem que ser idêntica.
- **`test_script.py`** - roda o `remote-link.sh` de verdade num sandbox (cópia
  em `tmp`, PATH só com dublês de `ffmpeg`/`openssl`/`python3`/`sudo`…). Cobre
  porta, TLS, guardian bloqueando, uinput sem permissão, modo `setup`.

### Cliente (`web/__tests__/`)

- **`app.test.js`** - por exemplo: decoder, áudio, mouse/touch/teclado, OSK,
  remap, UI, stats, boot.
- **`protocol.test.js`** - contrato e propriedade: todo comando enviado é um
  `t` que o servidor conhece, nenhum campo vira `null`/`NaN` no JSON, e
  `mapPad`/`readSource` produzem frame válido pra **qualquer** controle
  (incluindo perfil corrompido e pad sem arrays). É o espelho do
  `test_fuzz_input.py`.
- **`gamepad-debug.test.js`** - a página de diagnóstico: renderização e o
  histórico de min/max e oscilação (o detector de gatilho bugado).

### E2E (`web/e2e/`)

- **`smoke.spec.js`** - a UI carrega, conecta, o canvas recebe frame real,
  preset e som funcionam.
- **`api.spec.js`** - contrato HTTP contra o servidor real: formato de
  `/api/monitors`, ciclo completo do perfil de gamepad, 400 pra corpo inválido,
  página de debug, e lixo no `/ws/input` sem derrubar o canal.

## Escrevendo teste novo

**Ache a camada certa.** Lógica pura → unit. "As peças conversam?" → integração.
"Vale pra qualquer entrada?" → propriedade. Só no browser → e2e.

**Fuzz é determinístico.** Sementes fixas (`SEEDS = [...]`), nada de
`random.random()` sem semente nem `Date.now()`. Falhou uma vez, falha sempre -
senão o teste vira ruído e alguém desativa.

**Dublê só na borda.** Fake em `/dev/uinput`, `ffmpeg`, `xrandr`, `/proc` e
subprocess - o resto roda de verdade. Os fakes moram em `tests/helpers.py`
(Python) e `web/__tests__/setup.js` (JS); reaproveite antes de criar outro.

**Teste de robustez afirma sobrevivência, não formato.** O importante não é
"que erro deu" e sim que a conexão continua viva, o encoder continua rodando e
nada foi escrito fora da faixa.

**Função nova no cliente** que precise de teste tem que entrar no
`module.exports` do fim do `web/app.js` (o seam que permite testar sem bundler).

**Mexeu no protocolo, no `/api/*`, num preset ou numa `RL_*`?** Atualize
`CLAUDE.md`/`README.md` junto - o `tests/test_docs_sync.py` compara o que o
código expõe com o que a doc descreve e falha se divergir.
