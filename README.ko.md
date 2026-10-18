# ctfd-writeup-upload (한국어 안내)

CTFd에서 대회가 끝난 뒤 참가자가 **라이트업을 파일로 제출**하고 관리자가 내려받는 플러그인입니다. 자세한 설명은 [README](README.md)(영어)를 보세요.

- **참가자** (`/writeups`): `.pdf .zip .md .txt` 파일을 사용자당 최대 100개(파일당 20MB, 합계 200MB) 올리고, 내 파일 목록을 보고, 내 파일을 삭제합니다. 같은 내용의 파일을 다시 올리면 거절됩니다. 올린 파일은 참가자가 내려받을 수 없습니다.
- **관리자** (`/admin/writeups`): 참가자별로 묶인 목록, 파일별/사용자별 ZIP/전체 ZIP 다운로드, 삭제.
- **설치**: 저장소 전체가 플러그인입니다. `CTFd/plugins/`에서 `git clone https://github.com/deploy103/ctfd-writeup-upload.git writeups` 하고 CTFd를 재시작합니다. 기본 테마에서는 메뉴에 "Writeups" 링크가 자동으로 생기며 테마를 고칠 필요가 없습니다.
- **언어**: 기본은 영어입니다. 한국어로 쓰려면 `examples/ko/` 폴더를 `translations/ko/`로 복사하고 CTFd를 재시작하세요. 직접 고치려면 `messages.po`를 번역하고 `pybabel compile -f -d translations`로 `messages.mo`를 만들면 됩니다.
- MariaDB/MySQL을 권장합니다.
