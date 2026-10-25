# babelsh — 내 셸을 위한 바벨피시

**익숙한 명령어를 그대로 치세요. 지금 있는 OS에 맞게 실행됩니다.**

*프로젝트 이름은 **babelsh**, 터미널에서 치는 명령어는 **`ush`**(Universal SHell)입니다.*

Ubuntu에서 `apt`를, Mac에서 `brew`를, 회사 서버에서 `dnf`를 배웠고, `sed -i ''`는 한 번 데이고
나서야 외웠습니다. 그런데 다른 종류의 머신에 접속하는 순간 손이 먼저 틀린 명령을 칩니다.
`ush`는 셸 안에서 그걸 알아채고, 이 호스트의 네이티브 명령으로 바꿔 보여 주고, **당신이 허락한
뒤에만** 실행합니다.

```console
# Ubuntu에서, Fedora/macOS 손버릇으로
$ dnf install -y htop
ush: dnf install -y htop  ->  sudo apt install -y htop   [exact, modify]
Run? [y/N]
```

클라우드 호출도, AI 모델도 없습니다. 같은 입력은 항상 같은 결과를 내고, 모르는 것은 추측하지
않고 모른다고 말합니다.

> **상태: 알파(v0.0.1).** Ubuntu 22.04 + bash에서 동작하고 테스트되었습니다. **macOS 쪽은 문서를
> 근거로 구현했을 뿐 실제 Mac에서 돌려 본 적이 없습니다.** 규칙 범위도 의도적으로 아주 작습니다.
> 자세한 내용은 [README.md](README.md)의 *Status* 절을 보세요.

## 왜 만들었나

| 입력 | 문제 | ush가 하는 일 |
|---|---|---|
| `sudo apt install nginx` | macOS엔 `apt`가 없음 | `brew install nginx`로 바꾸고, Homebrew가 root를 거부하므로 `sudo`를 제거 |
| `apt install libssl-dev build-essential` | Mac에선 성격이 다른 두 작업 | `brew install openssl@3 && xcode-select --install` + `LDFLAGS` 안내 |
| `systemctl start nginx` | macOS엔 systemd가 없음 | `brew services start nginx` (사용자 단위 launchd라서 *approximate*로 표시) |
| `sed -i 's/a/b/' f` | BSD sed는 `-i ''`가 필요 | 양방향으로 변환 |
| `systemctl start ssh` | 대응 기능 없음 | *unsupported*라고 알리고 아무것도 실행하지 않음 |

## 무엇이 다른가

- **LLM이 아니라 규칙.** 읽을 수 있는 YAML 규칙을 JSON으로 컴파일해 씁니다. 결정적이고, 오프라인이고, 감사할 수 있습니다.
- **번역의 충실도를 알려 줍니다.** `exact / approximate / lossy / unsupported`와 위험도(`read / modify / destructive`)를 항상 표시하고, 파괴적 명령은 `yes`를 직접 입력해야 합니다.
- **추측하지 않고 거부합니다.** 모르는 패키지, 규칙에 없는 플래그, `$(…)`로 계산되는 인자, 방언이 모호한 경우에는 아무것도 실행하지 않고 이유를 알려 줍니다.
- **네이티브 명령은 건드리지 않습니다.** Debian의 `apt`처럼 호스트에 이미 있는 명령은 그대로 둡니다.
- **규칙은 서명된 데이터.** `ush update`는 고정된 키로 서명되었고, 설치된 것보다 새롭고, 서명된 해시와 일치하는 릴리스만 받습니다. 옛 릴리스로 되돌리는 공격도 막습니다.

## 지금 확인된 것 / 안 된 것

- **확인됨**(Ubuntu 22.04, 자동 테스트 107개 + 골든 30개): 파서, 규칙 검증, 번역 로직, bash 훅, 확인 프롬프트, 서명 업데이트 검증.
- **미확인**: **실제 Mac 전체**, zsh 훅, macOS 기본 bash 3.2(`command_not_found_handle` 없음), 공식 `minisign` 바이너리와의 호환.
- **범위**: 패키지 6개, 서비스 2개, 작업 6가지(install, remove, 인덱스 갱신, 서비스 시작, `sed -i`, 열린 포트 보기). 아직 `upgrade`, `search`, `journalctl`, `ip`, `free` 등은 없습니다.

## 도와주세요

1. **실제 Mac**에서 `python3 tests/run_golden.py`와 `bin/ush explain …`를 돌려 보고 다른 점을 이슈로 알려 주세요. 지금 가장 값진 기여입니다.
2. **규칙 추가**: 패키지 하나는 한 줄입니다 — `openssl_dev: { debian: libssl-dev, fedora: openssl-devel, macos: "openssl@3" }`
3. Fedora 사용자의 실사용 피드백.

## 써 보기

Python 3.8+ (표준 라이브러리만 사용). 아직 PyPI 패키지는 없습니다.

```sh
bin/ush hostinfo                      # 이 머신에서 감지한 정보
bin/ush explain dnf install -y htop   # 번역만 보여 주고 실행은 안 함
mkdir -p ~/.config/ush && bin/ush init bash > ~/.config/ush/hook.bash
echo 'source ~/.config/ush/hook.bash' >> ~/.bashrc
```

처음엔 `export USH_DRY_RUN=1`로 시작해 무엇을 실행할지 먼저 확인해 보세요.
설계 문서는 [`spec/SPEC.md`](spec/SPEC.md)에 있습니다.
