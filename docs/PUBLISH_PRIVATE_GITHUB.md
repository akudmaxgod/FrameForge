# Пошагово: приватный FrameForge на GitHub

## 1. Создать аккаунт и пустой репозиторий

1. Войдите в свой аккаунт на https://github.com.
2. Откройте https://github.com/new?name=FrameForge&visibility=private.
3. В поле **Repository name** оставьте `FrameForge`.
4. Выберите **Private**. Проверьте, что не выбран Public.
5. Не включайте создание README, .gitignore или лицензии: файлы уже подготовлены.
6. Нажмите **Create repository**.
7. Скопируйте адрес страницы вида `https://github.com/ВАШ-ЛОГИН/FrameForge`.
   Можно передать этот адрес агенту для дальнейшей загрузки. Пароль, токен и коды входа
   в чат присылать не нужно.

Основание: [официальная инструкция создания репозитория](https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository).

## 2. Загрузить подготовленные исходники

На GitHub загружается только папка **source** подготовленного выпуска, а не рабочая папка LIVE.
В ней нет пользовательских проектов, ключей, баз данных, виртуальных сред и моделей.

Удобный вариант без команд — GitHub Desktop:

1. Установите GitHub Desktop с https://desktop.github.com и войдите в свой аккаунт.
2. Если агент уже подготовил Git-репозиторий: **File → Add local repository** → папка `source`.
3. Если Git-репозитория ещё нет: **File → New repository**. Создайте локальный `FrameForge`
   в отдельной пустой папке. Скопируйте **содержимое source**, включая `.gitignore`,
   в этот локальный репозиторий. Не копируйте саму папку source ещё одним уровнем.
4. Во вкладке Changes убедитесь, что нет `data`, `.env`, `settings.json`, ключей,
   `.venv`, `backends`, `models`, архивов и личных изображений.
5. Сделайте **Commit to main** с описанием `Prepare FrameForge 0.2.0rc2`.
6. Если удалённый репозиторий уже создан в шаге 1: **Repository → Repository settings → Remote**
   и укажите его адрес `https://github.com/ВАШ-ЛОГИН/FrameForge.git`, затем Push.
   Если Desktop предлагает публикацию нового репозитория, обязательно оставьте
   **Keep this code private** включённым; не создавайте второй репозиторий с тем же именем.

Вариант через Git для подготовленной папки source:

```powershell
git init -b main
git add .
git status --short
git commit -m "Prepare FrameForge 0.2.0rc2"
git remote add origin https://github.com/ВАШ-ЛОГИН/FrameForge.git
git push -u origin main
```

Выполняйте команды из `source`. Если `origin` уже есть, сначала проверьте `git remote -v`.
Вход выполняйте через окно GitHub/Git Credential Manager. Не вставляйте токен в URL remote.
История старого LIVE-репозитория в новый выпуск не переносится.

## 3. Выложить готовую программу в Releases

1. В репозитории откройте **Releases → Draft a new release**.
2. Tag: `v0.2.0rc2`, Target: `main` с загруженным кодом.
3. Название: `FrameForge 0.2.0rc2 — закрытый тест`.
4. Вставьте раздел из `CHANGELOG.md` и ограничения из `docs/VALIDATION.md`.
5. Прикрепите из папки **artifacts**:
   - `FrameForge-0.2.0rc2-windows-x64.zip` — для пользователей;
   - `FrameForge-0.2.0rc2-source.zip` — очищенная копия исходников;
   - `SHA256SUMS.txt` — контрольные суммы архивов.
6. Включите **This is a pre-release** и нажмите **Save draft**, пока проверяется комплект файлов.
7. Для выдачи кандидата участникам пилота нажмите **Publish release**. Это закрытый тест,
   а не стабильный выпуск; стабильная версия выходит после пилота по `ROLLOUT.md`.

EXE, DLL, модели и базы не нужно добавлять в историю Git. Для программы используются
вложения Releases. [Официальная инструкция Releases](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository).

## 4. Дать доступ тестировщику

1. Откройте **Settings → Collaborators** (или **Collaborators and teams** у организации).
2. Нажмите **Add people**, укажите GitHub-логин тестировщика и отправьте приглашение.
3. Он должен принять приглашение, войти в GitHub и открыть страницу Releases.
4. Отправьте ему ссылку на выпуск и инструкцию из README.

Доступ к приватному репозиторию открывает также доступ к его исходникам. Если нужен доступ
только к программе без исходников, выдавайте архив отдельно либо используйте отдельный
приватный репозиторий дистрибутивов. Обычная ссылка private Releases не открывается анонимно.

## 5. Следующие обновления

Повторяйте чистую сборку → проверки → новая версия/тег → Release → обновление отдельной папкой.
В этом кандидате нет фонового автоматического скачивания из приватного GitHub.
Пользователь скачивает ZIP после входа. Это позволяет не встраивать ваш GitHub-токен
в программу и не раздавать его всем пользователям.

