# TEST-PLAN.md - bateria de testes do remote-link (meta: 100% de cobertura)

> Público deste documento: um agente (Claude) **mais econômico** implementando os
> testes. Siga **à risca** e na ordem. Quase todo o código difícil (fakes de
> subprocess, `/proc` falso, páginas Ogg) **já está pronto aqui** - copie.
>
> **Definição de pronto:** `pytest` passa e
> `pytest --cov=server --cov-report=term-missing --cov-fail-under=100` retorna
> **0 linhas faltando** em `server/*.py`.
>
> Este plano tem **duas partes**:
> - **PARTE A - backend Python** (seções 1–10): pytest, meta 100%.
> - **PARTE B - cliente web `web/app.js`** (seções 11–17): **Vitest** (jsdom,
>   dirige a cobertura, meta 100% do testável) + **Playwright** (browser real,
>   valida o que jsdom não consegue: WebCodecs/canvas/pointer-lock/fullscreen).

---

## 0. Regras de ouro (leia antes de escrever qualquer teste)

1. **NADA real é tocado.** Os testes **NUNCA** podem: abrir `/dev/uinput`, rodar
   `ffmpeg`, falar com o X11/PulseAudio, abrir socket de rede de verdade, abrir
   diálogo gráfico (`zenity`), nem matar o processo (`os._exit`). **Tudo isso é
   mockado.** Um teste que dependa do host está errado.
2. **Determinístico.** Sem `sleep` real, sem rede, sem tempo de parede. Onde o
   código dorme/espera, mocke `asyncio.sleep`.
3. **Cada teste mira linhas/branches específicos.** As seções abaixo listam
   exatamente o que cobrir. Se sobrar linha em `--cov-report=term-missing`,
   procure-a na checklist da seção 9.
4. **Async:** use `pytest-asyncio` em modo automático (já configurado na seção 1).
   Funções async de teso recebem `async def`.
5. Não altere `server/*.py` para "facilitar teste" - exceto se a seção 9 mandar
   explicitamente (não manda; o código é testável como está).

---

## 1. Setup do ambiente de teste

### 1.1 Instalar dependências de teste (no venv do projeto)

```bash
cd remote_link
./.venv/bin/pip install pytest pytest-asyncio pytest-cov
```

(Se não houver `.venv`, crie: `python3 -m venv --system-site-packages .venv && ./.venv/bin/pip install -r requirements.txt pytest pytest-asyncio pytest-cov`.)

### 1.2 Criar `pytest.ini` na raiz

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
addopts = -q
```

### 1.3 Criar `.coveragerc` na raiz

```ini
[run]
source = server
branch = True

[report]
show_missing = True
fail_under = 100
exclude_lines =
    pragma: no cover
    if __name__ == .__main__.:
```

> O `exclude_lines` do `__main__` é o único "atalho" permitido: cobre o corpo dos
> blocos `if __name__ == "__main__":` (em `guardian.py` e `main.py`) que não rodam
> sob import. **Todo o resto deve ser coberto por teste de verdade.**

### 1.4 Comando de cobertura (o gate final)

```bash
./.venv/bin/pytest --cov=server --cov-report=term-missing --cov-fail-under=100
```

---

## 2. Layout dos arquivos de teste

Crie a pasta `tests/` com:

```
tests/
  __init__.py        # vazio
  conftest.py        # fixtures globais + GOTCHAS de import (seção 3)
  helpers.py         # FakeProcess/FakeStream/FakeUInput/build_ogg_page/fake_proc (seção 4)
  test_keymap.py     # seção 5.1
  test_guardian.py   # seção 5.2  (MAIOR - é o código novo)
  test_inject.py     # seção 5.3
  test_video.py      # seção 5.4
  test_audio.py      # seção 5.5
  test_main.py       # seção 5.6
```

---

## 3. `tests/conftest.py` - GOTCHAS de import (CRÍTICO)

Importar `server.main` dispara, **no import**, duas coisas perigosas:
`guardian.enforce_preflight()` (que pode chamar `sys.exit`) e
`video.detect_encoder()` (que chama `ffmpeg`). Precisamos neutralizar **antes**
de qualquer import dos módulos do servidor.

Crie `tests/conftest.py` com EXATAMENTE isto no topo:

```python
import os
import sys

# --- DEVE rodar ANTES de importar qualquer server.* ---
# guardian off: importar server.main NÃO pode escanear/encerrar o processo.
os.environ["RL_GUARDIAN"] = "off"
# encoder forçado: video.detect_encoder() retorna na hora, sem chamar ffmpeg.
os.environ["RL_ENCODER"] = "x264"
# áudio/preset determinísticos
os.environ.setdefault("RL_AUDIO_PRESET", "media")

import pytest

# raiz do projeto no path (caso rode de fora)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Mantém guardian off e encoder x264 por padrão em todo teste.
    Testes que querem outro valor sobrescrevem com monkeypatch.setenv."""
    monkeypatch.setenv("RL_GUARDIAN", "off")
    monkeypatch.setenv("RL_ENCODER", "x264")
    yield
```

> Observação: alguns testes de `guardian` vão querer o guardian **ligado**. Eles
> fazem `monkeypatch.setenv("RL_GUARDIAN", "on")` localmente - ok, porque
> `guardian.disabled()` lê o env na hora da chamada.

---

## 4. `tests/helpers.py` - fakes prontos (COPIE INTEGRALMENTE)

Esta é a parte que economiza raciocínio. Cole este arquivo inteiro:

```python
"""Fakes deterministicos para evitar uinput/ffmpeg/proc reais nos testes."""
import asyncio


# ----------------------- subprocess assíncrono falso -----------------------
class FakeStream:
    """Simula proc.stdout (read coro) e proc.stderr (async-iterable de linhas)."""
    def __init__(self, chunks=None, lines=None):
        self._chunks = list(chunks or [])
        self._lines = list(lines or [])

    async def read(self, n=-1):
        if self._chunks:
            return self._chunks.pop(0)
        return b""               # EOF

    def __aiter__(self):
        self._it = iter(self._lines)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class FakeProcess:
    """Substitui asyncio.subprocess.Process."""
    def __init__(self, stdout_chunks=None, stderr_lines=None,
                 terminate_raises=False):
        self.stdout = FakeStream(chunks=stdout_chunks)
        self.stderr = FakeStream(lines=stderr_lines)
        self.returncode = None
        self._terminate_raises = terminate_raises
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True
        if self._terminate_raises:
            raise OSError("boom")
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        self.returncode = self.returncode or 0
        return self.returncode


def make_create_subprocess(processes):
    """Retorna um substituto de asyncio.create_subprocess_exec que entrega,
    em ordem, os FakeProcess de `processes` (repete o último se acabar)."""
    seq = list(processes)
    calls = {"n": 0}

    async def _fake(*args, **kwargs):
        calls["n"] += 1
        if seq:
            return seq.pop(0) if len(seq) > 1 else seq[0]
        return FakeProcess()
    _fake.calls = calls
    return _fake


# ----------------------------- UInput falso -----------------------------
class FakeUInput:
    """Captura writes/syn; substitui evdev.UInput em inject.py."""
    instances = []

    def __init__(self, *args, close_raises=False, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.writes = []      # lista de (etype, code, value)
        self.syns = 0
        self.closed = False
        self._close_raises = close_raises
        FakeUInput.instances.append(self)

    def write(self, etype, code, value):
        self.writes.append((etype, code, value))

    def syn(self):
        self.syns += 1

    def close(self):
        if self._close_raises:
            raise OSError("close boom")
        self.closed = True


# ----------------------------- páginas Ogg -----------------------------
def build_ogg_page(segments, header_type=0, serial=1, seqno=0):
    """Monta UMA página Ogg válida (CRC zerado - o parser ignora CRC).
    `segments`: lista de bytes; cada um vira 1+ lacing values automaticamente.
    Para um pacote que termina, o último lacing < 255."""
    seg_table = bytearray()
    body = bytearray()
    for seg in segments:
        n = len(seg)
        # quebra em lacings de 255
        while n >= 255:
            seg_table.append(255)
            n -= 255
        seg_table.append(n)       # lacing final (< 255) marca fim do pacote
        body += seg
    page = bytearray()
    page += b"OggS"
    page += bytes([0])            # version
    page += bytes([header_type])  # header_type (bit0 = continuation)
    page += bytes(8)              # granule pos
    page += serial.to_bytes(4, "little")
    page += seqno.to_bytes(4, "little")
    page += bytes(4)              # CRC (ignorado)
    page += bytes([len(seg_table)])
    page += bytes(seg_table)
    page += bytes(body)
    return bytes(page)


# ----------------------------- /proc falso (Linux) -----------------------------
def fake_proc(monkeypatch, guardian, procs):
    """Faz guardian._candidates_linux ler um /proc em memória.
    `procs`: { "123": {"comm": "x", "exe": "/a/b/x", "cmdline": ["x", "y.exe"]} }
    Use as chaves especiais "comm"/"exe"/"cmdline" com valor None (ou ausentes)
    para simular OSError naquele arquivo."""
    import os as _os

    monkeypatch.setattr(guardian.os, "listdir", lambda p: list(procs.keys()))

    def fake_readlink(path):
        pid = path.split("/")[2]
        exe = procs.get(pid, {}).get("exe")
        if exe is None:
            raise OSError("no exe")
        return exe
    monkeypatch.setattr(guardian.os, "readlink", fake_readlink)

    class FakeFile:
        def __init__(self, data): self._data = data
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self._data

    def fake_open(path, mode="rb"):
        parts = path.split("/")          # ['', 'proc', '123', 'comm']
        pid, name = parts[2], parts[3]
        rec = procs.get(pid, {})
        if name == "comm":
            v = rec.get("comm")
            if v is None: raise OSError("no comm")
            return FakeFile(v.encode())
        if name == "cmdline":
            cl = rec.get("cmdline")
            if cl is None: raise OSError("no cmdline")
            return FakeFile(("\x00".join(cl) + "\x00").encode())
        raise OSError("unknown")
    # `open` é builtin; setá-lo como atributo do módulo guardian o sombreia.
    monkeypatch.setattr(guardian, "open", fake_open, raising=False)
```

> **Por que `monkeypatch.setattr(guardian, "open", ...)`** funciona: Python
> resolve `open(...)` primeiro nos globais do módulo `guardian`, só depois nos
> builtins. Criar `guardian.open` sombreia o builtin **só** dentro de `guardian`.

---

## 5. Testes por módulo

Para cada módulo, escreva os testes listados. Os nomes são sugestões; o que
importa é cobrir os branches indicados. Rode `--cov-report=term-missing` por
arquivo (`--cov=server.guardian` etc.) enquanto desenvolve.

### 5.1 `test_keymap.py` (trivial - 100% só importando)

- `test_keymap_basico`: `from server.keymap import KEYMAP`; afirme
  `KEYMAP["KeyA"]`, `KEYMAP["Digit0"]`, `KEYMAP["F1"]`, `KEYMAP["Space"]`,
  `KEYMAP["ArrowUp"]`, `KEYMAP["Numpad0"]` existem e são `int`. O import já cobre
  100% do módulo (é só construção de dict).

### 5.2 `test_guardian.py` (o mais importante - código novo)

Importe: `from server import guardian` e `from tests.helpers import fake_proc`.

Cubra **cada** item:

**`_norm` / índice**
- `test_norm`: `guardian._norm("/A/B/Vgc.EXE") == "vgc.exe"`; lida com aspas:
  `guardian._norm('"x"') == "x"`.

**`Match.reason`**
- `test_match_reason_anticheat`: `Match("X","anticheat","vgc.exe","42").reason()`
  contém "Anticheat" e "vgc.exe".
- `test_match_reason_jogo`: idem com `kind="jogo"` contém "Jogo online".

**`_candidates_linux`** (use `fake_proc`)
- `test_candidates_linux_ok`: procs com comm/exe/cmdline normais → o `set` de um
  pid contém os basenames esperados (ex.: `"y.exe"` do cmdline).
- `test_candidates_linux_oserror_por_arquivo`: um pid com `comm=None`, outro com
  `exe=None`, outro com `cmdline=None` → não estoura, só ignora aquele arquivo
  (cobre os 3 `except OSError`).
- `test_candidates_linux_listdir_falha`: `monkeypatch.setattr(guardian.os,
  "listdir", lambda p: (_ for _ in ()).throw(OSError()))` → `list(...) == []`
  (cobre o `except OSError: return`). (Filtre só dígitos: inclua um pid não
  numérico tipo `"self"` nas chaves para cobrir o `if d.isdigit()`.)

**`_candidates_windows`**
- `test_candidates_windows_ok`: monkeypatch `guardian.subprocess.run` p/ retornar
  objeto com `.stdout` = CSV tipo `'"vgc.exe","42","Console","1","10 K"\r\n'`.
  Afirme que gera `("42", {"vgc.exe"})`.
- `test_candidates_windows_erro`: `subprocess.run` lança `OSError` → gera vazio.

**`_candidates_generic`**
- `test_candidates_generic_ok`: `subprocess.run` → `.stdout` = `"42 cs2\n  \n"` →
  gera `("42", {"cs2"})` e ignora linha em branco.
- `test_candidates_generic_erro`: lança → vazio.

**`_iter_processes`** (roteamento por plataforma)
- `test_iter_linux`/`win`/`darwin`: `monkeypatch.setattr(guardian.sys,
  "platform", "linux"/"win32"/"darwin")` e monkeypatch o `_candidates_*`
  correspondente para um sentinela; afirme que `list(_iter_processes())` usou o
  certo.

**`scan`** (monkeypatch `guardian._iter_processes`)
- `test_scan_anticheat_prioridade`: processos contendo um jogo **e** um anticheat
  → retorna o **anticheat** (early return).
- `test_scan_so_jogo`: só um jogo → retorna `Match` kind="jogo".
- `test_scan_limpo`: nenhum nome bate → `None`.
- `test_scan_match_real_basename`: garanta que `"rustc"` ou `"cargo"` **não**
  casa com o jogo Rust (anti-falso-positivo).

**`_banner`**
- `test_banner_preflight`: contém "INICIALIZACAO BLOQUEADA".
- `test_banner_runtime`: contém "ENCERRAMENTO DE EMERGENCIA".

**`_gui_notice`** (sempre mocke `subprocess.Popen`)
- `test_gui_linux_primeiro_falha_segundo_ok`: platform linux; monkeypatch
  `guardian.subprocess.Popen` com `side_effect=[OSError(), DummyPopen()]` →
  cobre o `except ... continue` e o `return` de sucesso.
- `test_gui_windows`: `monkeypatch.setattr(guardian.sys, "platform", "win32")`;
  injete `ctypes.windll` falso:
  ```python
  import ctypes, types
  fake = types.SimpleNamespace(user32=types.SimpleNamespace(
      MessageBoxW=lambda *a: 0))
  monkeypatch.setattr(ctypes, "windll", fake, raising=False)
  ```
  Afirme que não levanta. (Cobre o branch Windows + `return`.)
- `test_gui_windows_excecao_cai_no_loop`: faça `MessageBoxW` lançar; mocke
  `Popen` p/ sucesso → cobre o `except Exception: pass` do bloco Windows e a
  continuação no loop Linux.

**`show_block_notice`**
- `test_show_block_notice`: monkeypatch `guardian._gui_notice` (Mock); chame;
  capture `capsys`/`capfd` e afirme banner no stderr + `_gui_notice` chamado.

**`disabled`**
- `test_disabled_parametrizado`: `@pytest.mark.parametrize` com
  `("off",True),("0",True),("false",True),("nao",True),("on",False),("",False)`
  setando `RL_GUARDIAN` via `monkeypatch.setenv`.

**`enforce_preflight`**
- `test_preflight_desativado`: `RL_GUARDIAN=off` → retorna `None`, escreve aviso
  no stderr (capfd).
- `test_preflight_bloqueado`: `RL_GUARDIAN=on`; monkeypatch `guardian.scan` →
  `Match`; monkeypatch `guardian.show_block_notice` (Mock); use
  `pytest.raises(SystemExit) as ei` e afirme `ei.value.code == 3` e
  `show_block_notice` chamado.
- `test_preflight_limpo`: `RL_GUARDIAN=on`; `scan` → `None` → retorna `None`.

**`monitor`** (async)
- `test_monitor_desativado`: `RL_GUARDIAN=off` → `await monitor(trip)` retorna na
  hora sem chamar nada.
- `test_monitor_trip_apos_limpo`: `RL_GUARDIAN=on`. Monkeypatch:
  - `guardian.scan` com `side_effect=[None, Match("Y","jogo","cs2","9")]`
  - `guardian.show_block_notice` = Mock
  - `asyncio.sleep` por uma coro no-op que registra o argumento (p/ checar
    intervalo). `on_trip` = AsyncMock que registra o match.
  `await guardian.monitor(on_trip)`. Afirme: dormiu 1x (com 2.0), `show_block_notice`
  chamado 1x, `on_trip` chamado com o Match. (Cobre branch limpo→sleep→loop→trip.)
- `test_monitor_intervalo_env_valido`: `RL_GUARDIAN_INTERVAL=0.5`; primeira `scan`
  None depois Match; capture o arg de `asyncio.sleep` == 0.5.
- `test_monitor_intervalo_env_invalido`: `RL_GUARDIAN_INTERVAL=abc` → sleep com
  2.0 (cobre `except ValueError`).

> Dica p/ mockar `asyncio.sleep` sem dormir:
> ```python
> sleeps = []
> async def fake_sleep(d): sleeps.append(d)
> monkeypatch.setattr(guardian.asyncio if hasattr(guardian,'asyncio') else __import__('asyncio'),
>                     "sleep", fake_sleep)
> ```
> `guardian` faz `import asyncio` **dentro** de `monitor`; como é o módulo global
> `asyncio`, basta `monkeypatch.setattr(asyncio, "sleep", fake_sleep)` (importe
> `asyncio` no teste).

**`_main` (CLI)**
- `test_main_list`: `guardian._main(["--list"])` → `0`; `capsys` mostra
  "Anticheats:" e "Jogos:".
- `test_main_desativado`: `RL_GUARDIAN=off` → `_main([])` → `0`.
- `test_main_bloqueado`: `RL_GUARDIAN=on`; `scan`→Match; `show_block_notice`
  mock → `_main([]) == 3`.
- `test_main_limpo`: `RL_GUARDIAN=on`; `scan`→None → `_main([]) == 0`.

### 5.3 `test_inject.py`

No topo, **antes** de instanciar `Injector`, troque `UInput`:
```python
from server import inject
from tests.helpers import FakeUInput

@pytest.fixture
def inj(monkeypatch):
    FakeUInput.instances.clear()
    monkeypatch.setattr(inject, "UInput", FakeUInput)
    return inject.Injector()
```
Cubra:
- `test_init_cria_tres_devices`: após `Injector()`, `len(FakeUInput.instances)==3`.
- `test_set_geometry`: `inj.set_geometry((3840,1080), {"x":10,"y":0,"w":1920,"h":1080})`
  e afirme campos internos `_tw,_th,_region`.
- `test_move_abs_clamp`: `inj.move_abs(-1, 2)` → grava ABS_X/Y (clampados a 0/ABS_MAX)
  e `syn`. Inspecione `inj.pointer.writes`/`.syns`.
- `test_move_rel`: depois de `move_abs(0.5,0.5)`, `move_rel(0.1,0.1)` move relativo.
- `test_mouse_button_valido_e_invalido`: `mouse_button(0, True)` grava BTN_LEFT;
  `mouse_button(99, True)` → no-op (return cedo, nada gravado a mais).
- `test_wheel_sinais`: cubra `dy>0`, `dy<0`, `dx>0`, `dx<0` e `wheel(0,0)` (ainda
  dá `syn`, sem REL).
- `test_key_valido_e_invalido`: `key("KeyA", True)` grava; `key("NaoExiste", True)`
  → no-op.
- `test_gamepad_axes_completo`: `gamepad_state([0.5,-0.5,1,-1], [...])` cobre o
  `len(axes)>=4`. Inclua buttons cobrindo `bval`/`bpr` com índices presentes e
  ausentes (lista curta) → cobre os dois ternários (i<len e i>=len), o cálculo de
  HAT (`bpr(13)-bpr(12)` etc.) e o loop de `_PAD_BTN`.
- `test_gamepad_axes_curto`: `gamepad_state([0.0], [])` → NÃO entra no bloco de
  eixos (`len(axes)<4`) mas ainda escreve Z/RZ/HAT/botões.
- `test_handle_todos_os_t`: para cada `t` em `m,mr,md,mu,w,k,g` chame
  `inj.handle({...})` com payload válido; e `inj.handle({"t":"zzz"})` (no-op).
- `test_close_ok`: `inj.close()` fecha os 3 (`.closed True`).
- `test_close_swallows`: faça um device lançar no close
  (`FakeUInput(close_raises=True)` - troque um dos `inj.*` por um desses) e afirme
  que `close()` não levanta (cobre o `except Exception: pass`).

### 5.4 `test_video.py`

Importe `from server import video` e `from tests.helpers import FakeProcess,
FakeStream, make_create_subprocess`.

**`detect_encoder`** (chame direto, monkeypatchando deps de `video`)
- `test_detect_forcado`: `monkeypatch.setenv("RL_ENCODER","vaapi")` →
  `detect_encoder()=="vaapi"`.
- `test_detect_excecao`: del env; `monkeypatch.setattr(video.subprocess,"run",
  raise)`; `video.shutil.which`→None; `os.path.exists`→False → `"x264"`.
- `test_detect_nvenc`: run.stdout contém `"h264_nvenc"`; `which`→"/x/nvidia-smi"
  → `"nvenc"`.
- `test_detect_vaapi`: stdout `"h264_vaapi"`; `which`→None;
  `os.path.exists`→True → `"vaapi"`.
- `test_detect_x264`: stdout `""` → `"x264"`.

> Para apagar a env: `monkeypatch.delenv("RL_ENCODER", raising=False)`.

**`Preset.gop`**
- `test_preset_gop`: `video.PRESETS["high"].gop == 60`.

**`_x_env`**
- `test_x_env_com_xauth`: `setenv("RL_XAUTHORITY","/x")` → dict tem
  `XAUTHORITY=="/x"` e `DISPLAY`.
- `test_x_env_sem_xauth`: `delenv` → sem chave `XAUTHORITY` forçada (mas DISPLAY
  presente via default).

**`get_monitors`**
- `test_get_monitors_parse`: monkeypatch `video.subprocess.run` p/ `.stdout` com
  uma saída real de `xrandr --listmonitors`, ex.:
  ```
  Monitors: 2
   0: +*HDMI-0 1920/600x1080/340+0+0  HDMI-0
   1: +DP-1 1920/600x1080/340+1920+0  DP-1
  ```
  Afirme 2 monitores, `primary` True só no 0, coords corretas, e que a linha
  "Monitors: 2" (não casa a regex) é ignorada.
- `test_get_monitors_excecao`: run lança → `[]`.

**`virtual_screen`**
- `test_virtual_screen_com_mons`: monkeypatch `video.get_monitors` → 2 mons
  empilhados → retorna bounding box (max x+w, max y+h).
- `test_virtual_screen_vazio`: get_monitors → [] → `(1920,1080)`.

**`pick_region`**
- `test_pick_region_vazio`: get_monitors→[] → dict default "screen".
- `test_pick_region_por_indice`: índice existente → aquele monitor.
- `test_pick_region_indice_inexistente_cai_no_primario`: índice que não existe,
  mas há um `primary` → retorna o primário.
- `test_pick_region_primario`: `monitor_index=None`, há primário → primário.
- `test_pick_region_sem_primario`: nenhum primário → `mons[0]`.

**`_is_keyframe`**
- `test_keyframe_idr`: AU com NAL type 5 → True.
- `test_keyframe_sps`: NAL type 7 → True.
- `test_keyframe_nao`: AU só com NAL type 1 → False (cobre `sc<0` retorno e o
  loop avançando).
- `test_keyframe_curto`: AU `b"\x00\x00\x01"` (sem byte após) → cobre o ramo
  `sc+3 < len(au)` falso → `nt=0` → segue/None.

**`ScreenEncoder._build_cmd`** (monkeypatch `video.ENCODER`)
Para cada encoder, instancie `enc = video.ScreenEncoder(lambda *a: None)` e
chame `enc._build_cmd(preset, region)`:
- `test_build_nvenc_scale` (`video.ENCODER="nvenc"`, region h>max_h) → contém
  `"h264_nvenc"` e `"-vf"` com `scale`.
- `test_build_nvenc_sem_scale` (region h<=max_h) → contém `h264_nvenc`, sem
  `scale`.
- `test_build_vaapi_lowpower` (`ENCODER="vaapi"`, `enc._low_power=True`,
  scale_down True) → contém `h264_vaapi`, `scale_vaapi`, `-low_power`.
- `test_build_vaapi_sem_lowpower` (`enc._low_power=False`, scale_down False) →
  sem `-low_power`, sem `scale_vaapi`.
- `test_build_x264` (`ENCODER="x264"`, scale_down True) → contém `libx264`,
  `-vf scale`. E um caso scale_down False.

**`ScreenEncoder._read_stream`** (parser de AU)
- `test_read_stream_fatia_aus`: construa bytes com 2+ AUs delimitados por
  `b"\x00\x00\x01\x09"`. Use:
  ```python
  AUD = b"\x00\x00\x01\x09"
  data = AUD + b"\x00\x00\x01\x05frame1" + AUD + b"\x00\x00\x01\x01frame2" + AUD
  ```
  `enc.proc = FakeProcess(stdout_chunks=[data])`; `enc._stop=False`; colete em
  `recebidos=[]` via callback async. `await enc._read_stream()`. Afirme que dois
  AUs saíram, o 1º com `is_key=True`. (Cobre o loop, `find`, o `del buf`, e o
  ramo `j<0 → del buf[:i]/break` se você mandar um chunk incompleto seguido de
  EOF.)
- `test_read_stream_au_incompleto`: mande `[AUD + b"\x00\x00\x01\x05parcial"]`
  (sem AUD final) → nenhum AU completo emitido, retorna no EOF (cobre `j<0` com
  `i>0`).

**`_drain_stderr`**
- `test_drain_stderr`: `enc.proc = FakeProcess(stderr_lines=[b"erro x\n",
  b"  \n"])`; `await enc._drain_stderr()`; capture stdout (print) → "erro x"
  aparece. E `test_drain_stderr_sem_proc`: `enc.proc=None` → retorna sem erro.

**`start`/`stop`/`_supervise`** (mocke `asyncio.create_subprocess_exec` e
`asyncio.sleep`)
- `test_stop_sem_proc`: `enc.stop()` com `proc=None`, `_task=None` → não levanta.
- `test_stop_termina`: `enc.proc=FakeProcess()` (returncode None),
  `enc._task=<task dummy concluída>` → `await enc.stop()`; afirme
  `proc.terminated`.
- `test_stop_terminate_falha_mata`: `FakeProcess(terminate_raises=True)` →
  `enc.stop()` chama `kill()` (afirme `proc.killed`). (Cobre o `except: kill`.)
- `test_supervise_x264_limite_tentativas`: `monkeypatch.setattr(video.ENCODER...)`
  ou melhor `monkeypatch.setattr(video, "ENCODER", "x264")`; monkeypatch
  `asyncio.create_subprocess_exec` por `make_create_subprocess([FakeProcess()])`
  (stdout EOF imediato → `_read_stream` retorna já); monkeypatch `asyncio.sleep`
  no-op. `enc.start(preset, region); await enc._task`. Afirme que rodou 6x e
  parou (`attempts>5 break`).
- `test_supervise_vaapi_downgrade`: `video.ENCODER="vaapi"`, `enc._low_power=True`;
  mesmo esquema (EOF imediato, sleep no-op). Afirme `enc._low_power` virou
  `False` (cobre `ran<2.0 and vaapi and low_power → continue`). Garanta término
  pelo `attempts>5`.
- `test_supervise_stop_no_meio`: faça a 1ª `_read_stream` setar `enc._stop=True`
  (use um FakeProcess cujo stdout, ao ser lido, marque stop - ou monkeypatch
  `enc._read_stream` por uma coro que faz `enc._stop=True`) → cobre o
  `if self._stop: break`.

### 5.5 `test_audio.py`

Importe `from server import audio` e helpers.

**Constantes / dataclass**
- `test_audio_presets`: `audio.AUDIO_PRESETS["voz"].application == "voip"`.

**`default_monitor`**
- `test_default_monitor_forcado`: `setenv("RL_AUDIO_SOURCE","mysrc")` → "mysrc".
- `test_default_monitor_token`: `delenv` → `audio.DEFAULT_MONITOR_TOKEN`.

**`list_monitors`**
- `test_list_monitors_parse`: monkeypatch `audio.subprocess.run` p/ retornar obj
  com `.stderr` contendo linhas tipo
  `"  * alsa_output.pci.analog-stereo.monitor [Monitor of ...]"` e uma linha sem
  `.monitor` → só as `.monitor` entram.
- `test_list_monitors_excecao`: run lança → `[]`.

**`audio_available`**
- `test_available_forcado`: `setenv("RL_AUDIO_SOURCE","x")` → True.
- `test_available_sem_ffmpeg`: `delenv`; `monkeypatch.setattr(audio.shutil,
  "which", lambda *_:None)` → False.
- `test_available_com_monitor`: `delenv`; `which`→"/x/ffmpeg"; monkeypatch
  `audio.list_monitors`→`[{...}]` → True. E uma versão com `list_monitors`→[] →
  False.

**`OggOpusParser`** (use `build_ogg_page`)
- `test_ogg_headers_descartados_e_pacote`: feed de uma página com
  `[b"OpusHead..."]`, depois `[b"OpusTags..."]`, depois `[b"DADOS"]`. Afirme que
  só `b"DADOS"` sai. (Cobre `_take_packet` descartando 2 headers.)
- `test_ogg_pacote_cruza_paginas`: pacote dividido em 2 páginas via lacing 255 +
  página com `header_type=0x01` (continuação). Construa manualmente um segmento
  de 300 bytes (gera lacing 255 + 45) na 1ª página e a continuação na 2ª. Afirme
  remontagem correta. (Cobre `self._partial`, `lacing==255`, `header_type&0x01`.)
- `test_ogg_reset_orfao`: página de continuação **sem** partial pendente
  (`header_type` sem bit0 após um partial) → o `_partial` é zerado (cobre o
  `if not (header_type & 0x01): self._partial = bytearray()`).
- `test_ogg_buffer_incompleto`: feed de um prefixo de página (< header) →
  `_next_page` retorna None e nada é emitido; depois feed do resto → emite.
  (Cobre os vários `if len(buf) < ...: return None`.)
- `test_ogg_lixo_antes_do_oggs`: feed `b"LIXO" + page` → casa `start>0`, descarta
  prefixo.
- `test_ogg_sem_oggs_trim`: feed `b"XXXXXXXX"` (sem "OggS", >4 bytes) → mantém só
  a cauda (cobre `del buf[:-4]`).
- `test_ogg_take_packet_vazio`: garanta um caminho com pacote vazio → ignorado
  (lacing 0). 
- `test_ogg_take_packet_pos_headers_raro`: force `_headers_left>0` mas o pacote
  não é OpusHead/Tags (ex.: parser novo recebendo dados direto) → cobre
  `self._headers_left = 0; packets.append(pkt)`.

**`AudioEncoder`** (espelha video)
- `test_audio_build_cmd`: `enc=audio.AudioEncoder(cb)`; `_build_cmd(preset)`
  contém `"libopus"`, `"-page_duration","20000"`, `enc.source`.
- `test_audio_read_stream`: `enc.proc=FakeProcess(stdout_chunks=[ogg_bytes])`
  onde `ogg_bytes` = headers + 1 pacote (via `build_ogg_page`); colete pacotes no
  callback. Afirme o pacote de dados entregue.
- `test_audio_drain_stderr` / `sem_proc`: igual ao video.
- `test_audio_stop_*`: igual ao video (sem proc / termina / terminate falha →
  kill).
- `test_audio_supervise_limite`: create_subprocess EOF imediato, sleep no-op →
  6x e `attempts>5 break`. E `test_audio_supervise_stop`: `_stop` no meio.

### 5.6 `test_main.py`

Antes de importar `server.main`, garanta os GOTCHAS (conftest já cuida). Mocke
`Injector` e os encoders para os testes de app/rotas.

```python
import json, asyncio, ssl as ssl_mod
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from server import main as M, video, audio
from tests.helpers import FakeUInput
```

**`VideoClient`**
- `test_videoclient_feed_inativo_nao_key`: cliente novo (`active=False`),
  `feed(False, au)` → fila vazia (dropado).
- `test_videoclient_feed_key_ativa`: `feed(True, au)` → ativa e enfileira.
- `test_videoclient_feed_queuefull`: encha a fila (maxsize 180) e force
  `QueueFull` → `_flush` + `active=False`.
- `test_videoclient_reset`: enfileira algo, `reset()` → flush + msg de reset na
  fila. E force `QueueFull` no put do reset (fila cheia) → cobre o `except
  QueueFull: pass` (encha a fila a 180 antes do reset… na prática teste o ramo
  com um `Queue(maxsize=…)` monkeypatchado ou pulando - ver dica abaixo).
- `test_videoclient_sender_bin_e_txt`: crie `ws` fake com `send_bytes`/`send_str`
  AsyncMock; ponha `("bin", b"x")` e `("txt","y")` na fila; rode `sender()` numa
  task e cancele após 2 itens. Afirme ambos enviados.

> Dica p/ o `QueueFull` do reset: instancie `c=VideoClient(ws)` e
> `c.q = asyncio.Queue(maxsize=1)`; `c.q.put_nowait(("x",b""))`; então `c.reset()`
> chama `_flush()` (esvazia) e consegue pôr o reset → para forçar o except, em vez
> disso monkeypatche `c._flush` para no-op e deixe a fila cheia.

**`AudioClient`**
- `test_audioclient_feed`: `feed(b"a")` enfileira.
- `test_audioclient_feed_full_descarta_antigo`: `c.q=Queue(maxsize=1)`; encha;
  `feed(b"novo")` → descarta o antigo e põe o novo.
- `test_audioclient_feed_full_queueempty`: cubra o `except QueueEmpty` (fila que
  esvazia entre o get e o put - pode monkeypatchar `q.get_nowait` p/ lançar
  `QueueEmpty`).
- `test_audioclient_sender`: como o do video.

**`Hub`** (injector e encoder mockados)
```python
def make_hub(monkeypatch):
    inj = mock.Mock()
    hub = M.Hub(inj)
    hub.encoder = mock.Mock()
    hub.encoder.start = mock.Mock()
    hub.encoder.stop = mock.AsyncMock()
    monkeypatch.setattr(video, "ScreenEncoder", lambda cb: _mk_async_encoder())
    return hub, inj
```
- `test_hub_preset_valido_invalido`: `hub.preset_name="zzz"` → `hub.preset`
  retorna o default (cobre o `.get(..., default)`).
- `test_hub_region`: monkeypatch `video.pick_region` → afirme repassa.
- `test_hub_on_au_fanout`: adicione 2 `VideoClient` (mock feed) → `_on_au` chama
  feed em ambos.
- `test_hub_add_first_inicia`: `await hub.add_client(c)` com `_running False` →
  chama `encoder.start`, `_running True`, e `_sync_geometry` (injector.set_geometry
  chamado).
- `test_hub_add_segundo_nao_reinicia`: segundo add não chama start de novo.
- `test_hub_remove_ultimo_para`: remove o único → `encoder.stop` chamado,
  `_running False`.
- `test_hub_remove_nao_ultimo`: com 2 clientes, remover 1 não para.
- `test_hub_restart_rodando` e `test_hub_restart_parado`: chame `await
  hub._restart()` nos dois estados (cobre o `if not self._running: ... return`).
  No estado rodando, troca o encoder e reinicia (monkeypatch `video.ScreenEncoder`).
- `test_hub_set_preset_muda` / `igual` / `invalido`: só muda+restart quando válido
  e diferente.
- `test_hub_set_monitor_muda` / `igual`.

**`AudioHub`** (análogo)
- `test_audiohub_preset_fallback`, `add_first`, `add_segundo`, `remove_ultimo`,
  `remove_nao_ultimo`, `set_preset_rodando` (para+recria encoder),
  `set_preset_parado` (só troca nome), `set_preset_invalido/igual`.

**Rotas / app** (com `aiohttp.test_utils`)
Fixture que monta o app com Injector e encoders falsos:
```python
@pytest.fixture
async def client(monkeypatch, aiohttp_client):
    FakeUInput.instances.clear()
    monkeypatch.setattr(M, "Injector", lambda: _FakeInjector())
    monkeypatch.setattr(video, "ScreenEncoder", lambda cb: _mk_async_encoder())
    monkeypatch.setattr(audio, "AudioEncoder", lambda cb: _mk_async_encoder())
    app = M.build_app()
    return await aiohttp_client(app)
```
> `_FakeInjector` tem `set_geometry`, `handle`, `close` (Mocks). `_mk_async_encoder`
> retorna objeto com `start` (Mock) e `stop` (AsyncMock).
> **Importante:** o `aiohttp_client` dispara `on_startup` → cria a task
> `guardian.monitor`. Como `RL_GUARDIAN=off` (conftest), ela retorna na hora. OK.

- `test_index`: `GET /` → 200, content-type html.
- `test_api_monitors`: monkeypatch `video.get_monitors`, `audio.audio_available`,
  `audio.list_monitors` p/ valores fixos; `GET /api/monitors` → JSON com
  `encoder`, `monitors`, `presets`, `audio_presets`, etc.
- `test_ws_video`: `ws = await client.ws_connect("/ws/video")`; feche
  (`await ws.close()`) → cobre add/remove client no finally.
- `test_ws_audio`: idem.
- `test_ws_input_comandos`: conecte `/ws/input`; envie `{"t":"q","preset":"low"}`,
  `{"t":"aq","preset":"baixa"}`, `{"t":"mon","i":1}`, `{"t":"mon","i":null}`,
  um comando de input `{"t":"m","x":0.5,"y":0.5}` (afirme `injector.handle`
  chamado), um JSON inválido (texto não-JSON → `continue`), e force exceção no
  `injector.handle` (Mock com side_effect) p/ cobrir o `except` que imprime
  `[input] erro`. Feche o ws.
- `test_ws_input_nao_texto`: envie um frame binário → cobre o ramo
  `msg.type != TEXT` que não é CLOSE/ERROR (`continue`).

**`build_app` cleanup + `_trip` (runtime trip)**
- `test_on_cleanup`: ao fechar o client/app, `injector.close` e `encoder.stop`
  são chamados (cobre `on_cleanup` e `stop_guardian`).
- `test_trip_runtime` (cobre o closure `_trip` + `guardian.monitor` real):
  ```python
  monkeypatch.setenv("RL_GUARDIAN", "on")
  monkeypatch.setattr(M.guardian, "disabled", lambda: False)
  monkeypatch.setattr(M.guardian, "scan",
                      lambda: M.guardian.Match("J","jogo","cs2","1"))
  monkeypatch.setattr(M.guardian, "show_block_notice", lambda *a: None)
  called = {}
  def fake_exit(code): called["code"] = code; raise SystemExit(code)
  monkeypatch.setattr(M.os, "_exit", fake_exit)
  ```
  Suba o app com encoders/injector falsos, deixe a task `guardian.monitor` rodar
  (faça `await asyncio.sleep(0)` algumas vezes ou aguarde a task). Afirme
  `called["code"] == 99`, `encoder.stop`/`injector.close` chamados. (Use
  `pytest.raises`/captura porque `_exit` levanta `SystemExit` dentro da task -
  capture via `task` ou trate a exceção da task.)

> Alternativa mais simples e robusta para `_trip`: capture o app e chame o
> handler manualmente. Como `_trip` é closure interno, prefira o caminho acima
> (deixar `guardian.monitor` chamá-lo). Se ficar difícil sincronizar a task,
> reduza ruído: na fixture, **não** rode `monitor` para os outros testes e, só
> neste, construa `app = M.build_app()` e dispare `on_startup` manualmente,
> aguardando `app["guardian_task"]`.

**`main()`** (mocke `web.run_app` e ssl)
- `test_main_sem_tls`: `delenv RL_CERT/RL_KEY`; `monkeypatch.setattr(M.web,
  "run_app", lambda *a, **k: None)`; mocke `M.Injector`/encoders; chame
  `M.main()`. Afirme aviso "sem TLS" no stderr e `run_app` chamado com
  `ssl_context=None`.
- `test_main_com_tls`: crie 2 arquivos temporários (cert/key) com `tmp_path`;
  `setenv RL_CERT/RL_KEY`; `monkeypatch.setattr(ssl_mod, "SSLContext", FakeCtx)`
  onde `FakeCtx.load_cert_chain` é no-op; `run_app` no-op. Afirme `run_app`
  chamado com `ssl_context` não-None. (Cobre o ramo do TLS.)

---

## 6. Ordem sugerida de implementação

1. `helpers.py` + `conftest.py` + `pytest.ini` + `.coveragerc`.
2. `test_keymap.py` (sanidade do ambiente).
3. `test_guardian.py` (maior valor - código novo).
4. `test_inject.py`.
5. `test_video.py` e `test_audio.py` (reutilizam `FakeProcess`).
6. `test_main.py`.

Rode a cobertura por módulo enquanto avança:
```bash
./.venv/bin/pytest tests/test_guardian.py --cov=server.guardian --cov-report=term-missing
```

---

## 7. Como provar 100%

Rode o gate final:
```bash
./.venv/bin/pytest --cov=server --cov-report=term-missing --cov-fail-under=100
```
Saída esperada: todos os arquivos de `server/` com **`100%`** e a coluna
`Missing` vazia. Se `--cov-fail-under=100` falhar, o `term-missing` aponta as
linhas - vá à seção 9.

Gere também HTML para inspeção fina, se precisar:
```bash
./.venv/bin/pytest --cov=server --cov-report=html && xdg-open htmlcov/index.html
```

---

## 8. Erros comuns (e como evitar)

- **`SystemExit` ao importar `server.main`**: você esqueceu `RL_GUARDIAN=off` no
  topo do `conftest.py` **antes** dos imports. Veja seção 3.
- **Testes tentando rodar ffmpeg/abrir uinput**: faltou monkeypatch de
  `create_subprocess_exec` / `Injector` / `UInput`. Nenhum subprocess real deve
  subir.
- **Diálogo gráfico abrindo de verdade**: faltou mockar `subprocess.Popen` em
  testes de `_gui_notice`/`show_block_notice`.
- **Teste travando**: algum `await` sem dado (FakeStream sem EOF) ou
  `asyncio.sleep` real num `_supervise`. Garanta EOF (`b""`) e `sleep` no-op.
- **`os._exit` matando o pytest**: SEMPRE monkeypatch `M.os._exit` antes de
  exercitar `_trip`/`monitor` ligados.
- **Cobertura "quase 100%" no `__main__`**: confirme o `exclude_lines` do
  `.coveragerc` (seção 1.3).

---

## 9. Checklist de linhas tradicionalmente difíceis (mapa → teste)

| Arquivo | Trecho difícil | Teste que cobre |
|---|---|---|
| guardian | 3× `except OSError` em `_candidates_linux` | `test_candidates_linux_oserror_por_arquivo` |
| guardian | `except OSError` do `os.listdir` | `test_candidates_linux_listdir_falha` |
| guardian | branch Windows do `_gui_notice` + `except` | `test_gui_windows`, `test_gui_windows_excecao_cai_no_loop` |
| guardian | loop zenity→…→xmessage (falha+sucesso) | `test_gui_linux_primeiro_falha_segundo_ok` |
| guardian | early-return anticheat no `scan` | `test_scan_anticheat_prioridade` |
| guardian | `enforce_preflight` `sys.exit(3)` | `test_preflight_bloqueado` |
| guardian | `monitor`: limpo→sleep→trip, parse de intervalo | `test_monitor_trip_apos_limpo`, `*_intervalo_*` |
| inject | `except Exception: pass` no `close` | `test_close_swallows` |
| inject | ternários `bval/bpr` (i<len vs i>=len) | `test_gamepad_axes_completo` + `_curto` |
| video | `_read_stream` ramo `j<0`/`i>0` | `test_read_stream_au_incompleto` |
| video | `_supervise` `attempts>5` e downgrade vaapi | `test_supervise_x264_limite_tentativas`, `_vaapi_downgrade` |
| video | `stop` terminate falha → kill | `test_stop_terminate_falha_mata` |
| audio | `OggOpusParser` continuação 255 / `_partial` | `test_ogg_pacote_cruza_paginas` |
| audio | `_take_packet` ramo "pós-headers raro" | `test_ogg_take_packet_pos_headers_raro` |
| audio | reset de órfão (`header_type & 0x01` falso) | `test_ogg_reset_orfao` |
| main | `VideoClient.feed` QueueFull | `test_videoclient_feed_queuefull` |
| main | `AudioClient.feed` QueueEmpty | `test_audioclient_feed_full_queueempty` |
| main | `_trip` (stop tudo + `os._exit(99)`) | `test_trip_runtime` |
| main | `main()` com e sem TLS | `test_main_sem_tls`, `test_main_com_tls` |

---

## 10. Saída final esperada

```
$ ./.venv/bin/pytest --cov=server --cov-report=term-missing --cov-fail-under=100
...
Name                     Stmts   Miss Branch BrPart  Cover   Missing
--------------------------------------------------------------------
server/__init__.py           0      0      0      0   100%
server/audio.py            ...      0    ...      0   100%
server/guardian.py         ...      0    ...      0   100%
server/inject.py           ...      0    ...      0   100%
server/keymap.py           ...      0      0      0   100%
server/main.py             ...      0    ...      0   100%
server/video.py            ...      0    ...      0   100%
--------------------------------------------------------------------
TOTAL                      ...      0    ...      0   100%

Required test coverage of 100% reached. Total coverage: 100.00%
====================== N passed in X.XXs ======================
```

Quando esse retorno aparecer com `Required test coverage of 100% reached`, a
**PARTE A** está concluída. Siga para a **PARTE B** (cliente web).

---
---

# PARTE B - Cliente web `web/app.js` (Vitest + Playwright)

`web/app.js` é **JS puro, sem bundler** (convenção do projeto - mantenha assim).
É um *script clássico*: sem `export`, funções no escopo do módulo, e chama
`boot()` no fim (que toca `fetch`, `requestAnimationFrame`, `getGamepads`). Quase
tudo depende de APIs de browser (WebCodecs, Canvas2D, WebSocket, AudioContext,
Gamepad, Pointer Lock, Fullscreen) que **não existem no Node/jsdom**.

Divisão de trabalho:
- **Vitest (jsdom)** → dirige a **cobertura**. Testa a lógica pura e a lógica de
  fluxo com **stubs** das APIs de browser. Meta: **100% do que é testável**, com
  uma lista curta e documentada de linhas *browser-only* ignoradas (seção 14).
- **Playwright (Chromium real)** → **valida de verdade** o caminho que jsdom não
  reproduz: decode WebCodecs real desenhando no canvas, pointer lock, fullscreen,
  gamepad real. Não conta pra cobertura de linha; é o teste de aceitação visual.

---

## 11. Seam mínimo no `web/app.js` (única edição permitida no cliente)

Para o Vitest **importar** `app.js` (e o V8 instrumentar/mapear cobertura ao
arquivo) sem disparar `boot()` nem perder acesso às funções, adicione **no final**
do arquivo, substituindo a linha `boot();`:

```js
// ---------- boot / test seam ----------
// Em browser, `module` não existe -> roda normal. Sob Vitest/Node (CommonJS),
// expõe as internals para teste e NÃO auto-executa boot(). Não muda o runtime
// real do cliente (sem bundler, sem import).
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    hex2, findNALs, codecFromSPS, ensureDecoder, resetDecoder, onAU,
    startAudioContext, ensureAudioDecoder, resetAudioDecoder, playAudio,
    onAudioPacket, connectAudio, setAudio, connect, connectVideo, connectInput,
    sendInput, flushMove, normFromEvent, pollPads, setStatus, charToCode,
    loadConfig, statsLoop, boot,
    // getters para o estado de módulo que os testes precisam inspecionar:
    __get: (k) => eval(k),
  };
} else {
  boot();
}
```

> Por que `__get`: várias variáveis são `let` de módulo (`decoder`, `audioOn`,
> `playHead`, `padSnap`, `pendingMove`, `configured`…). O `eval(k)` dentro do
> escopo do módulo permite ler `__get("audioOn")` no teste. (Só leitura; não
> precisamos setar.)
>
> **Importante:** essa é a **única** mudança em `web/app.js`. Não converta para
> ESM, não adicione bundler. Em produção (browser) o `else { boot(); }` roda igual
> a antes.

Se o revisor recusar QUALQUER edição no cliente: use a alternativa da seção 16
(harness por `vm`/indirect-eval, sem seam) - funciona, mas o mapeamento de
cobertura ao arquivo fica menos direto.

---

## 12. Setup Vitest

### 12.1 `package.json` (na raiz; só devDeps de teste)

```json
{
  "name": "remote-link-web-tests",
  "private": true,
  "type": "commonjs",
  "scripts": {
    "test:web": "vitest run --coverage",
    "test:e2e": "playwright test"
  },
  "devDependencies": {
    "vitest": "^2.1.0",
    "@vitest/coverage-v8": "^2.1.0",
    "jsdom": "^25.0.0",
    "@playwright/test": "^1.48.0"
  }
}
```

Instale:
```bash
cd remote_link
npm install
npx playwright install chromium     # baixa o Chromium do Playwright
```

### 12.2 `vitest.config.js`

```js
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["web/__tests__/**/*.test.js"],
    setupFiles: ["web/__tests__/setup.js"],
    coverage: {
      provider: "v8",
      include: ["web/app.js"],
      reporter: ["text", "html"],
      thresholds: { lines: 100, functions: 100, branches: 100, statements: 100 },
    },
  },
});
```

> Se a meta de 100% não fechar por linhas *browser-only* legítimas (seção 14),
> marque-as com `/* v8 ignore next */` no `app.js` (comentário, não quebra o
> cliente) **ou** baixe o threshold de `branches` para o valor real documentado.
> Prefira `v8 ignore` pontual a baixar threshold global.

---

## 13. Harness de DOM + stubs - `web/__tests__/setup.js` (COPIE)

`app.js` referencia, no topo e nos listeners, vários elementos por id e várias
APIs globais. O setup precisa fornecê-los **antes** de importar `app.js`.

```js
import { beforeEach, vi } from "vitest";

// ---- DOM mínimo com TODOS os ids que app.js usa ----
const IDS = ["screen", "overlay", "status", "stats", "connect", "lock", "full",
  "kbd", "menuToggle", "sound", "preset", "monitor", "audioPreset",
  "hiddenKbd", "bar", "audioGroup"];

export function mountDOM() {
  document.body.innerHTML = "";
  for (const id of IDS) {
    const el = id === "screen" ? document.createElement("canvas")
             : (["preset", "monitor", "audioPreset"].includes(id) ? document.createElement("select")
             : (id === "hiddenKbd" ? document.createElement("input") : document.createElement("div")));
    el.id = id;
    document.body.appendChild(el);
  }
  // canvas 2d não existe no jsdom -> stub leve
  const cv = document.getElementById("screen");
  cv.getContext = () => ({ drawImage: vi.fn() });
  cv.requestPointerLock = vi.fn();
  cv.getBoundingClientRect = () => ({ left: 0, top: 0, width: 1920, height: 1080 });
}

// ---- stubs de APIs de browser ----
export function stubBrowser() {
  // WebSocket fake controlável
  globalThis.__sockets = [];
  globalThis.WebSocket = class {
    constructor(url) { this.url = url; this.readyState = 1; this.sent = [];
      globalThis.__sockets.push(this); }
    send(d) { this.sent.push(d); }
    close() { this.readyState = 3; this.onclose && this.onclose(); }
  };
  // WebCodecs
  globalThis.VideoDecoder = class {
    constructor(cfg) { this.cfg = cfg; this.state = "unconfigured"; VideoDecoderCalls.push(this); }
    configure(c) { this.state = "configured"; this.configured = c; }
    decode(chunk) { this.decoded = (this.decoded || []); this.decoded.push(chunk); }
    close() { this.state = "closed"; }
  };
  globalThis.AudioDecoder = class {
    constructor(cfg) { this.cfg = cfg; this.state = "unconfigured"; }
    configure(c) { this.state = "configured"; }
    decode(chunk) { this.decoded = (this.decoded || []); this.decoded.push(chunk); }
    close() { this.state = "closed"; }
  };
  globalThis.EncodedVideoChunk = class { constructor(o) { Object.assign(this, o); } };
  globalThis.EncodedAudioChunk = class { constructor(o) { Object.assign(this, o); } };
  // AudioContext
  globalThis.AudioContext = class {
    constructor() { this.state = "running"; this.currentTime = 0; this.destination = {}; }
    createBuffer(ch, n, sr) { return { duration: n / sr,
      copyToChannel: vi.fn() }; }
    createBufferSource() { return { connect: vi.fn(), start: vi.fn(), buffer: null }; }
    resume() { this.state = "running"; }
    suspend() { this.state = "suspended"; }
  };
  // rAF: executa 1x e para (evita loop infinito); testes chamam manualmente
  globalThis.requestAnimationFrame = vi.fn();
  globalThis.performance = globalThis.performance || { now: () => 0 };
  navigator.getGamepads = () => [];
  globalThis.fetch = vi.fn(async () => ({ json: async () => ({
    presets: ["high"], current_preset: "high", monitors: [], current_monitor: null,
    audio_available: false, audio_presets: [], current_audio_preset: "media" }) }));
}

export const VideoDecoderCalls = [];

// carrega app.js LIMPO a cada teste (sem auto-boot por causa do seam)
export async function loadApp() {
  VideoDecoderCalls.length = 0;
  vi.resetModules();
  return await import("../app.js");
}

beforeEach(() => {
  mountDOM();
  stubBrowser();
});
```

> Notas:
> - `globalThis.WebSocket` guarda cada instância em `__sockets` para o teste
>   inspecionar `.sent`, disparar `.onmessage(...)`, `.onclose()`, `.onerror()`.
> - `requestAnimationFrame` é `vi.fn()` (não reentra) - assim `boot()`/`flushMove`/
>   `pollPads`/`statsLoop`/`flushMove` não entram em loop. Onde o teste quiser
>   exercitar o corpo, chama a função uma vez e checa o efeito.

---

## 14. Testes Vitest por função (mapa de cobertura)

Crie `web/__tests__/app.test.js`. Importe via `loadApp()` dentro de cada teste
(ou num `beforeEach`), e use `app.__get("x")` para ler estado de módulo.

**Puras (fáceis, 100% trivial)**
- `hex2`: `hex2(5)==="05"`, `hex2(255)==="ff"`.
- `findNALs`: buffer com 2 start codes → retorna types/hdr corretos; sem start
  code → `[]`; cobre o `i += 2`.
- `codecFromSPS`: dado SPS sintético → `"avc1.xxyyzz"` correto.
- `charToCode`: `"a"→"KeyA"`, `"5"→"Digit5"`, `" "→"Space"`, `"!"→null`.

**`onAU` (configura decoder + decode)**
- `test_onAU_configura_no_SPS`: AU com NAL 7 (SPS) + 5 (IDR) → cria
  `VideoDecoder`, chama `configure` com `codec` avc1.* e `decode` com `type:"key"`.
- `test_onAU_sem_sps_antes_de_config`: `configured=false` e AU sem SPS → `return`
  cedo (nada decodificado).
- `test_onAU_delta_apos_config`: depois de configurado, AU só com NAL 1 →
  `decode` com `type:"delta"`.
- `test_onAU_decode_lanca_ressync`: faça `decoder.decode` lançar → cai no
  `catch`, chama `resetDecoder` (cheque `app.__get("decoder")===null`).

**`ensureDecoder` / `resetDecoder`**
- `test_ensureDecoder_reusa`: chama 2x → não recria se `state!=="closed"`.
- `test_resetDecoder_fecha`: com decoder configurado → `close()` chamado e estado
  zerado; e a versão com `decoder.close` lançando (cobre `catch (e) {}`).
- `test_ensureDecoder_output_callback`: invoque o `output(frame)` do cfg passado
  ao `VideoDecoder` com um `frame` fake (`displayWidth/Height`, `close: vi.fn`) →
  ajusta `canvas.width/height`, chama `drawImage`, `frame.close`, atualiza
  `decodeLatency` (cobre o ramo `if (lastFrameAt)`); chame 2x p/ cobrir os dois
  lados do `if`.
- `test_decoder_error_callback`: invoque o `error(e)` do cfg → `resetDecoder`.

**Áudio: `startAudioContext` / `ensureAudioDecoder` / `resetAudioDecoder`**
- `startAudioContext`: 1ª vez cria `AudioContext`; com `state==="suspended"`
  chama `resume`. Cubra o fallback `window.webkitAudioContext` (force
  `window.AudioContext=undefined`, defina `window.webkitAudioContext`).
- `ensureAudioDecoder`: cria e `configure({codec:"opus"})`; reusa se não closed.
- `resetAudioDecoder`: fecha (e versão com close lançando), zera `playHead`.

**`playAudio`** (vários branches de reâncora)
- `test_playAudio_sem_ctx`: `audioCtx` null → `data.close()` e `return`.
- `test_playAudio_normal`: data fake (`numberOfFrames`, `numberOfChannels`,
  `copyTo`, `close`) → cria buffer, copia canais, `src.start(playHead)`.
- `test_playAudio_copyTo_fallback`: 1ª `copyTo` (com opts) lança → usa o `catch`
  com `copyTo(tmp,{planeIndex})`.
- `test_playAudio_reancora_atrasado`: `playHead < now+0.005` → reancora.
- `test_playAudio_reancora_rajada`: `playHead > now + JITTER + 0.12` → reancora.

**`onAudioPacket`**
- decode key chunk + `audioTs += 20000`; e o `catch` (decode lança →
  `resetAudioDecoder`).

**`connectAudio` / `setAudio`**
- `setAudio(true)`: `audioGroup.hidden=false`, texto/classe do botão, chama
  `startAudioContext`, `connectAudio` (cria socket `/ws/audio`).
- `setAudio(false)`: fecha socket, `resetAudioDecoder`, `audioCtx.suspend`.
- `connectAudio` com `audioOn=false` → `return` cedo (chame `setAudio(false)`
  antes, ou manipule via fluxo). Dispare `ws.onmessage` (com `audioOn` true) →
  `onAudioPacket`; `ws.onclose` → reconnect agendado (cheque
  `setTimeout`/`__sockets`); `ws.onerror` → `close`.

**Conexão vídeo/input**
- `connect`: esconde overlay, chama `connectInput` + `connectVideo`.
- `connectVideo.onmessage` string `{"t":"reset"}` → `resetDecoder`; string
  inválida → `catch`; binário → `onAU`. `onclose` → `resetDecoder` + status +
  setTimeout (mocke `document.visibilityState`). `onerror` → `close`.
- `connectInput.onclose` → setTimeout reconnect.
- `sendInput`: com socket `readyState===1` envia JSON; com socket null/!=1 não
  envia (cubra os dois).

**Mouse/touch/teclado/roda (dispatch de eventos no jsdom)**
- `normFromEvent`: clampa 0..1 com o rect mockado.
- `mousemove` sem pointer lock → seta `pendingMove` (`t:"m"`); com
  `document.pointerLockElement===canvas` → envia `t:"mr"`.
- `flushMove`: com `pendingMove` setado → envia e zera.
- `mousedown`/`mouseup`/`contextmenu`/`wheel`: dispare eventos e cheque
  `sendInput` (`md/mu/w`) e `preventDefault`.
- `touchstart/move/end`: 1 toque → `m`; arraste seta `pendingMove`; `touchend`
  sem mover → `md` + `mu` (use timers fake do Vitest p/ o `setTimeout(40)`).
- `keydown/keyup`: com input pronto envia `k`; `code==="F11"` → ignora; sem
  socket pronto → ignora.

**Gamepad**
- `pollPads`: `getGamepads` retorna 1 pad com axes/buttons → envia `t:"g"` e
  dedup por `padSnap` (chamar 2x com mesmo estado não reenvia; mudar reenvia).
  Sem pad → não envia. (Cobre `p.axes[i] || 0` e o `.map`.)
- `gamepadconnected`: dispare evento → `setStatus` com id cortado.

**UI / listeners de botões**
- Clique em `connect`, `lock` (`requestPointerLock`), `full` (fullscreen:
  mocke `document.fullscreenElement`, `requestFullscreen`, `exitFullscreen` nos
  dois ramos), `kbd` (`hiddenKbd.focus`), `menuToggle` (toggle classe), `sound`
  (chama `setAudio`).
- `preset`/`monitor`/`audioPreset` `change` → envia `q`/`mon`/`aq`. Para
  `monitor`, cubra `value===""` (→ `i:null`) e numérico.
- `hiddenKbd` `input`: `e.data` letra → envia down+up; `e.data` null → nada;
  caractere sem mapa → nada.

**`loadConfig` (popula selects)**
- `fetch` mockado retornando config com presets/monitors/áudio → preenche
  `#preset`/`#monitor`/`#audioPreset`, marca selected, habilita `#sound`.
  Cubra: `audio_available && "AudioDecoder" in window` (true) e o `else`
  (desabilita botão, seta `title` nos dois sub-casos). `fetch` lançando →
  `catch (console.warn)`.

**`statsLoop`**
- Force `performance.now` p/ `dt>=1`: cheque `#stats.textContent` com fps/Mbps/ms,
  e o ramo `audioOn` (adiciona kbps). E `dt<1` → não atualiza.

**`boot`**
- `test_boot_sem_webcodecs`: `delete globalThis.VideoDecoder` →
  status "Sem WebCodecs…", `#connect.disabled=true`, `return` (não chama
  loadConfig). Chame `app.boot()` manualmente (o seam não auto-roda).
- `test_boot_normal`: com `VideoDecoder` presente → chama `loadConfig`, status
  "pronto…", e agenda os loops (cheque `requestAnimationFrame`/`fetch` chamados).

**Lista de linhas a marcar `/* v8 ignore */` (browser-only, vão pro Playwright)**
Só se não fechar 100% por elas - normalmente NÃO precisa, pois tudo acima é
stubável. Candidatas legítimas: nenhuma essencial. Se aparecer alguma linha de
`requestFullscreen`/`requestPointerLock` impossível de cobrir no jsdom, ignore-a
pontualmente e cubra no Playwright (seção 15).

### Gate Vitest
```bash
npm run test:web
```
Espere `web/app.js ... 100%` em lines/branches/functions/statements.

---

## 15. Playwright - e2e em Chromium real (validação que jsdom não dá)

Objetivo: provar que, num **browser de verdade**, o cliente conecta, carrega
config, e o pipeline WebCodecs realmente decodifica e **desenha no canvas**.
Roda contra o servidor real (com guardian OFF e encoder definido), **no host com
X11/GPU** (estes testes são *host-dependent* - documente isso; num CI headless
sem display o vídeo não gera frames).

### 15.1 `playwright.config.js`

```js
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "web/e2e",
  timeout: 30000,
  use: {
    baseURL: "https://127.0.0.1:18443",
    ignoreHTTPSErrors: true,            // cert self-signed
    headless: true,
  },
  webServer: {
    command: "RL_GUARDIAN=off RL_PORT=18443 RL_CERT=certs/cert.pem RL_KEY=certs/key.pem ./.venv/bin/python -m server.main",
    url: "https://127.0.0.1:18443",
    ignoreHTTPSErrors: true,
    reuseExistingServer: true,
    timeout: 20000,
  },
});
```

> Pré-requisitos do host: `certs/` gerados (rode `./remote-link.sh` uma vez, ou
> gere o cert), X11 acessível (o script normal exporta `RL_DISPLAY`/
> `RL_XAUTHORITY` - no Playwright, exporte-os no `command` se necessário),
> ffmpeg/uinput como em produção. **Guardian OFF** aqui é proposital: é ambiente
> de teste controlado, não uso real.

### 15.2 `web/e2e/smoke.spec.js`

```js
import { test, expect } from "@playwright/test";

test("carrega UI e popula config", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#status")).not.toHaveText("");
  // /api/monitors populou o select de preset
  await expect(page.locator("#preset option")).not.toHaveCount(0);
});

test("conecta e o canvas recebe frames (WebCodecs real)", async ({ page }) => {
  await page.goto("/");
  await page.click("#connect");
  // espera o canvas ganhar dimensões reais (decoder.output ajusta width/height)
  await expect.poll(async () =>
    page.evaluate(() => document.getElementById("screen").width),
    { timeout: 15000 }
  ).toBeGreaterThan(0);
});

test("troca de preset envia comando", async ({ page }) => {
  await page.goto("/");
  await page.click("#connect");
  await page.selectOption("#preset", { index: 0 });
  // sem erro no console é o mínimo; idealmente espie o WS /ws/input no server
  const errors = [];
  page.on("pageerror", (e) => errors.push(e));
  await page.waitForTimeout(500);
  expect(errors).toEqual([]);
});
```

> O 2º teste é o que **não dá pra fazer no Vitest**: exige WebCodecs+GPU reais
> decodificando o H.264 do ffmpeg e pintando o canvas. É a prova visual de que o
> caminho ponta-a-ponta funciona.

### 15.3 Rodar
```bash
npm run test:e2e
# ou um único: npx playwright test web/e2e/smoke.spec.js
```

---

## 16. Alternativa SEM editar `app.js` (se o seam for recusado)

Se não puder tocar no cliente, carregue o fonte e avalie-o em escopo global via
*indirect eval*, expondo as function declarations no `globalThis` do jsdom:

```js
import fs from "node:fs";
export async function loadAppNoSeam() {
  let src = fs.readFileSync(new URL("../app.js", import.meta.url), "utf8");
  src = src.replace(/\nboot\(\);\s*$/, "\n");   // remove o auto-boot
  (0, eval)(src);                                // declarations -> globalThis
  return globalThis;                             // findNALs, onAU, ... viram globais
}
```
Trade-off: a cobertura V8 não mapeia bem o código `eval`-ado de volta ao arquivo,
então o **gate de 100% por arquivo fica frágil**. Por isso o **seam da seção 11 é
o caminho recomendado**. Use esta alternativa só como último recurso e, nesse
caso, baseie a cobertura na suíte de testes (qualidade) em vez do número por
arquivo.

---

## 17. Critério de pronto da PARTE B

1. `npm run test:web` → `web/app.js` com **100%** (ou o threshold documentado na
   seção 14 com `v8 ignore` justificado), todos os testes verdes.
2. `npm run test:e2e` → no host com X11/GPU, os 3 specs passam (em especial o do
   canvas recebendo frames). Em CI headless sem display, documente que o spec de
   frames é *skipped* (`test.skip(!process.env.DISPLAY, "precisa de X11")`).

E o `.gitignore` deve ignorar `node_modules/`, `coverage/`, `htmlcov/`,
`playwright-report/`, `test-results/`, `.pytest_cache/`.
