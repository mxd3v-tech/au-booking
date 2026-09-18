#!/bin/bash
# Сквозная проверка в отдельном стеке: рабочая БД и .env не используются.
set -u
CD=$(cd "$(dirname "$0")/.." && pwd)
for tool in docker curl python3 node; do
  command -v "$tool" >/dev/null || { echo "Нужен $tool для самопроверки" >&2; exit 1; }
done
PROJECT="au-queue-test-$(date +%s)-$$"
compose=(docker compose --env-file /dev/null --project-directory "$CD"
         -f "$CD/scripts/compose.test.yml" -p "$PROJECT")
TMP=$(mktemp -d) || exit 1
cleanup(){
  local result=$?
  trap - EXIT
  if [ "$result" != 0 ]; then "${compose[@]}" logs --no-color app; fi
  "${compose[@]}" down --volumes --remove-orphans || result=1
  rm -rf -- "$TMP"
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"${compose[@]}" up -d --build --wait --wait-timeout 90 || exit 1
PORT=$("${compose[@]}" port app 8000) || exit 1
BASE="http://$PORT"
A="$TMP/admin"; U1="$TMP/user1"; U2="$TMP/user2"; U3="$TMP/user3"; S="$TMP/response"
touch "$A" "$U1" "$U2" "$U3" "$S" || exit 1
pass=0; fail=0
ok(){ echo "  ok   $1"; pass=$((pass+1)); }
no(){ echo "  FAIL $1"; fail=$((fail+1)); }
sql(){ "${compose[@]}" exec -T db psql -U au_queue -d au_queue_test -v ON_ERROR_STOP=1 -t -A -c "$1"; }
ADMIN_USER=smoke-admin
ADMIN_PASS=smoke-password

code(){ curl -s -o /dev/null -w '%{http_code}' "$@"; }

echo "== 0. Чистая площадка"
n=$(sql "select count(*) from queue_session") || exit 1
[ "$n" = 0 ] && ok "отдельная тестовая БД пуста" || { no "тестовая БД не пуста"; exit 1; }

echo "== 1. Вход в админку"
c=$(code -c "$A" -X POST "$BASE/admin/login" \
  --data-urlencode "username=$ADMIN_USER" --data-urlencode "password=$ADMIN_PASS")
[ "$c" = 303 ] && ok "логин 303" || no "логин вернул $c"
c=$(code -b "$A" "$BASE/admin"); [ "$c" = 200 ] && ok "панель доступна" || no "панель вернула $c"
c=$(code "$BASE/admin/sessions"); [ "$c" = 303 ] && ok "без входа редирект на логин" || no "защита админки: $c"
c=$(code -X POST "$BASE/admin/login" \
  --data-urlencode "username=$ADMIN_USER" --data-urlencode "password=пароль-с-кириллицей")
[ "$c" = 401 ] && ok "кириллический пароль → 401, а не 500" || no "неверный пароль: $c"

echo "== 2. Приём закрыт"
curl -s "$BASE/" | grep -q "Сейчас приёма нет" && ok "студент видит «приёма нет»" || no "нет надписи о закрытом приёме"
c=$(code "$BASE/join"); [ "$c" = 303 ] && ok "встать в очередь нельзя" || no "/join при закрытом приёме: $c"

echo "== 3. Открытие приёма"
c=$(code -b "$A" -X POST "$BASE/admin/session/open" \
  --data-urlencode "room=1215" --data-urlencode "time_from=14:00" --data-urlencode "time_to=16:00" \
  --data-urlencode "note=Сегодня только пересдачи")
[ "$c" = 303 ] && ok "приём открыт" || no "открытие вернуло $c"
SID=$(sql "select id from queue_session where closed_at is null")
[ -n "$SID" ] && ok "сеанс №$SID открыт" || no "открытого сеанса нет"
r=$(sql "insert into queue_session (room, note) values ('999','')" 2>&1)
echo "$r" | grep -q "uq_session_open" && ok "второй приём отклонён базой" || no "база пустила два приёма: $r"

# без -X POST: иначе curl повторит POST и после редиректа, а нам нужен GET
curl -s -L -b "$A" "$BASE/admin/session/update" \
  --data-urlencode "room=1215" --data-urlencode "time_from=16:00" --data-urlencode "time_to=14:00" > "$S"
grep -q "позже начала" "$S" && ok "конец раньше начала не принимается" || no "кривое окно принято"
# возвращаем как было — форма шлёт все поля разом, и заметку в том числе
curl -s -o /dev/null -b "$A" "$BASE/admin/session/update" \
  --data-urlencode "room=1215" --data-urlencode "time_from=14:00" --data-urlencode "time_to=16:00" \
  --data-urlencode "note=Сегодня только пересдачи"

echo "== 4. Главная страница студента"
curl -s "$BASE/" > "$S"
grep -q "Приём идёт" "$S" && ok "видно, что приём идёт" || no "нет отметки о приёме"
grep -q "каб. 1215" "$S" && ok "кабинет 1215 показан" || no "кабинет не показан"
grep -q "14:00–16:00" "$S" && ok "время приёма показано" || no "времени приёма не видно"
grep -q "Сегодня только пересдачи" "$S" && ok "заметка показана" || no "заметки нет"
grep -q "Встать в очередь" "$S" && ok "кнопка записи на месте" || no "нет кнопки записи"

# Ответ любого типа задания так, как его отправил бы браузер.
solve(){ sql "select kind || '|' || answer::text from captcha_challenge where id='$1'" | python3 -c '
import sys, json
kind, raw = sys.stdin.read().strip().split("|", 1)
a = json.loads(raw)
if "values" in a:
    print(",".join("1" if v else "0" for v in a["values"]) if kind == "truth_myth"
          else ",".join(str(v) for v in a["values"]))
elif "node" in a:
    print(a["node"])
elif "prefix" in a:
    print(a["prefix"])
else:
    print(a["index"])'; }
cid_of(){ grep -o 'data-id="[^"]*"' "$1" | head -1 | cut -d'"' -f2; }
field(){ python3 -c 'import sys,json;d=json.load(sys.stdin)
for key in sys.argv[1:]: d = d.get(key) or {}
print(d if isinstance(d, str) else "")' "$@"; }

# Типы берём из админки: добавится новый — он сразу попадёт в проверку.
curl -s -b "$A" "$BASE/admin/captcha" > "$S"
KINDS=$(grep -o 'data-captcha-kind="[^"]*"' "$S" | cut -d'"' -f2)
echo "== 5. Капча: все типы ($(echo "$KINDS" | wc -w | tr -d ' ') шт.)"
for kind in $KINDS; do
  curl -s -b "$A" "$BASE/admin/captcha?kind=$kind" > "$S"
  cid=$(cid_of "$S")
  grep -q "data-kind=\"$kind\"" "$S" || no "$kind: админка показала не тот тип"
  grep -q '"answer"' "$S" && no "$kind: правильный ответ утёк в HTML!" || ok "$kind: ответа нет в разметке"
  r=$(curl -s -X POST "$BASE/api/captcha/$cid" --data-urlencode "answer=$(solve "$cid")")
  echo "$r" | grep -q '"ok":true' && ok "$kind: верный ответ принят" || no "$kind: $r"
done

echo "== 6. Капча: одна попытка, разбор и замена другого типа"
curl -s -b "$A" "$BASE/admin/captcha?kind=quiz" > "$S"; cid=$(cid_of "$S")
right=$(solve "$cid"); wrong=$(( (right + 1) % 4 ))
r=$(curl -s -X POST "$BASE/api/captcha/$cid" -d "answer=$wrong")
echo "$r" | grep -q '"ok":false' && ok "неверный ответ отклонён" || no "неверный принят: $r"
len=$(echo "$r" | python3 -c 'import sys,json;print(len(json.load(sys.stdin).get("text","")))')
[ "$len" -gt 40 ] && ok "есть разбор ошибки ($len симв.)" || no "разбор пустой"
next_kind=$(echo "$r" | field replacement kind)
[ -n "$next_kind" ] && [ "$next_kind" != "quiz" ] \
  && ok "на замену сразу пришёл другой тип ($next_kind)" || no "замены другого типа нет: $r"
echo "$r" | field replacement html | grep -q "data-kind=\"$next_kind\"" \
  && ok "замена пришла готовой разметкой" || no "в замене нет разметки задания"
r=$(curl -s -X POST "$BASE/api/captcha/$cid" --data-urlencode "answer=$right")
echo "$r" | grep -q '"expired":true' && ok "после одной ошибки задание сгорает" || no "вторая попытка сработала: $r"
other=$(curl -s "$BASE/api/captcha?previous=$cid" | field kind)
[ -n "$other" ] && [ "$other" != "quiz" ] && ok "«Другое задание» меняет тип ($other)" \
  || no "кнопка выдала тот же тип: $other"

PID=$(sql "select id from purpose where needs_comment = false order by sort_order limit 1")
PID_C=$(sql "select id from purpose where needs_comment = true order by sort_order limit 1")

join(){ # jar, фамилия имя, группа, [цель], [комментарий]
  local jar="$1" name="$2" grp="$3" purpose="${4:-$PID}" note="${5:-}"
  curl -s -b "$jar" "$BASE/join" > "$S"
  local cid; cid=$(grep -o 'name="captcha_id" value="[^"]*"' "$S" | cut -d'"' -f4)
  curl -s -o /dev/null -X POST "$BASE/api/captcha/$cid" --data-urlencode "answer=$(solve "$cid")"
  code -b "$jar" -c "$jar" -X POST "$BASE/join" \
    --data-urlencode "full_name=$name" --data-urlencode "group_name=$grp" \
    --data-urlencode "purpose_id=$purpose" --data-urlencode "comment=$note" \
    --data-urlencode "captcha_id=$cid" --data-urlencode "session_id=$SID"
}

echo "== 7. Проверки формы"
curl -s -b "$U1" "$BASE/join" > "$S"
cid=$(grep -o 'name="captcha_id" value="[^"]*"' "$S" | cut -d'"' -f4)
c=$(code -X POST "$BASE/join" --data-urlencode "full_name=Иванов Иван" \
  --data-urlencode "group_name=КТ-24-04" --data-urlencode "purpose_id=$PID" \
  --data-urlencode "captcha_id=$cid" --data-urlencode "session_id=$SID")
[ "$c" = 422 ] && ok "без решённой капчи не пускает" || no "капча не обязательна: $c"
c=$(join "$U3" "Иванов" "КТ-24-04"); [ "$c" = 422 ] && ok "одна фамилия без имени не проходит" || no "имя не проверяется: $c"
c=$(join "$U3" "Сидоров Пётр" "КТ2404"); [ "$c" = 422 ] && ok "кривая группа не проходит" || no "группа не проверяется: $c"
c=$(join "$U3" "Сидоров Пётр" "КТ-24-04" "$PID_C" ""); [ "$c" = 422 ] && ok "цель с обязательным пояснением требует его" || no "пояснение не требуется: $c"

echo "== 8. Очередь набирается"
c=$(join "$U1" "Иванов Иван" "КТ-24-04" "$PID" "лабораторная 4")
[ "$c" = 303 ] && ok "первый студент записался" || no "запись вернула $c"
n=$(sql "select number from queue_entry where full_name='Иванов Иван'")
[ "$n" = 1 ] && ok "выдан номер 1" || no "номер первого: $n"
c=$(join "$U2" "Петрова Анна" "АИ-23-04")
n=$(sql "select number from queue_entry where full_name='Петрова Анна'")
[ "$n" = 2 ] && ok "второму выдан номер 2" || no "номер второго: $n"

# U3 — «другой телефон»: своей записи у него нет, куки чистые.
c=$(join "$U3" "Иванов Иван" "КТ-24-04")
[ "$c" = 422 ] && ok "повтор той же фамилии и группы отклонён" || no "дубль прошёл: $c"
c=$(join "$U3" "иванов  иван" "КТ-24-04")
[ "$c" = 422 ] && ok "регистр и лишние пробелы не помогают" || no "дубль через регистр: $c"
c=$(join "$U3" "Иван Иванов" "КТ-24-04")
[ "$c" = 422 ] && ok "переставленные имя и фамилия не помогают" || no "дубль через порядок слов: $c"
c=$(join "$U3" "Иванов Иван" "КТ-24-09")
[ "$c" = 422 ] && ok "другая группа не помогает" || no "дубль через группу: $c"
c=$(join "$U3" "Иванов Иван Петрович" "КТ-24-04")
[ "$c" = 422 ] && ok "дописанное отчество не помогает" || no "дубль через отчество: $c"

# Ключ берём из базы, а не зашиваем: тест проверяет ограничение, а не формат.
K=$(sql "select full_name_key from queue_entry where full_name='Иванов Иван'")
r=$(sql "insert into queue_entry (session_id, number, full_name, full_name_key, group_name, comment, status, token, added_by_admin, ip_hash)
         values ($SID, 99, 'Иванов Иван', '$K', 'КТ-24-04', '', 'done', 'dubl-test', false, '')" 2>&1)
echo "$r" | grep -q "uq_entry_person" && ok "дубль отклонён базой, а не только формой" || no "база пустила дубль: $r"

echo "== 9. Имена видны всем"
curl -s "$BASE/" > "$S"
grep -q "Иванов Иван" "$S" && ok "фамилия первого видна без кук" || no "имена не показываются"
grep -q "Петрова Анна" "$S" && ok "фамилия второго видна" || no "второго не видно"
grep -q "АИ-23-04" "$S" && ok "группа видна" || no "группы не видно"
grep -q "лабораторная 4" "$S" && no "комментарий утёк в общий список!" || ok "комментарий чужим не показан"

echo "== 10. Свой номерок"
curl -s -b "$U2" "$BASE/" > "$S"
grep -q "Ваш номер" "$S" && ok "свой номер показан" || no "нет блока «Ваш номер»"
grep -q "Перед вами: 1 человек" "$S" && ok "склонение «1 человек» верное" || no "не та фраза о числе впереди"
r=$(curl -s -b "$U2" "$BASE/api/queue")
echo "$r" | grep -q '"waiting": *2' && ok "api отдаёт 2 ожидающих" || no "api: $r"
echo "$r" | grep -q '"mine": *2' && ok "api знает мой номер" || no "api не видит мою запись"

echo "== 11. Выход из очереди"
c=$(code -b "$U2" -X POST "$BASE/leave"); [ "$c" = 303 ] && ok "выход принят" || no "выход вернул $c"
st=$(sql "select status from queue_entry where full_name='Петрова Анна'")
[ "$st" = "left" ] && ok "статус «ушёл» проставлен" || no "статус после выхода: $st"
n=$(sql "select count(*) from queue_entry where session_id=$SID and status='waiting'")
[ "$n" = 1 ] && ok "в очереди остался один" || no "ожидающих: $n"

echo "== 12. Отметки преподавателя"
EID=$(sql "select id from queue_entry where full_name='Иванов Иван'")
c=$(code -b "$A" -X POST "$BASE/admin/entries/$EID/status" -d "status=done" -d "back=/admin")
[ "$c" = 303 ] && ok "отметка «принят» принята" || no "отметка вернула $c"
st=$(sql "select status from queue_entry where id=$EID")
[ "$st" = "done" ] && ok "статус в базе — принят" || no "статус: $st"
curl -s -b "$U1" "$BASE/" | grep -q "приём состоялся" && ok "студент видит, что его приняли" || no "студенту не видно отметки"
c=$(join "$U3" "Иванов Иван" "КТ-24-04")
[ "$c" = 422 ] && ok "после «принят» второй раз не записаться" || no "повтор после приёма прошёл: $c"

echo "== 13. Запись вручную"
c=$(code -b "$A" -X POST "$BASE/admin/entries" \
  --data-urlencode "full_name=Кузнецов Олег" --data-urlencode "group_name=КС-23-04" \
  --data-urlencode "purpose_id=$PID")
[ "$c" = 303 ] && ok "ручная запись создана" || no "ручная запись: $c"
n=$(sql "select number from queue_entry where full_name='Кузнецов Олег'")
[ "$n" = 3 ] && ok "ей достался номер 3" || no "номер ручной записи: $n"
c=$(code -b "$A" -X POST "$BASE/admin/entries" \
  --data-urlencode "full_name=Иванов Иван" --data-urlencode "group_name=КС-23-04" \
  --data-urlencode "purpose_id=$PID")
n=$(sql "select count(*) from queue_entry where session_id=$SID and full_name='Иванов Иван'")
[ "$n" = 1 ] && ok "вручную дубль тоже не завести" || no "админка завела дубль: записей $n"
c=$(code -b "$A" -X POST "$BASE/admin/entries" \
  --data-urlencode "full_name=Иванов Иван" --data-urlencode "group_name=КС-23-04" \
  --data-urlencode "purpose_id=$PID" --data-urlencode "namesake=on")
n=$(sql "select number from queue_entry where full_name='Иванов Иван' and group_name='КС-23-04'")
[ "$n" = 4 ] && ok "однофамилец по галочке прошёл, номер 4" || no "однофамилец не прошёл: $n"

echo "== 14. Выгрузки и печать"
curl -s -b "$A" "$BASE/admin/sessions/$SID/export.xlsx" -o "$S"
head -c2 "$S" | grep -q "PK" && ok "xlsx — настоящий zip" || no "xlsx не похож на zip"
python3 - "$S" <<'PY' && ok "в xlsx есть фамилия" || no "xlsx без фамилии"
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
blob = b"".join(z.read(n) for n in z.namelist() if n.endswith(".xml"))
sys.exit(0 if "Иванов Иван".encode() in blob else 1)
PY
curl -s -b "$A" "$BASE/admin/sessions/$SID/export.csv" -o "$S"
head -c3 "$S" | od -An -tx1 | grep -q "ef bb bf" && ok "csv с BOM для Excel" || no "csv без BOM"
grep -q "Кузнецов Олег" "$S" && ok "в csv есть ручная запись" || no "csv без ручной записи"
c=$(code -b "$A" "$BASE/admin/sessions/$SID/print")
[ "$c" = 200 ] && ok "печатная форма открывается" || no "печать вернула $c"

echo "== 15. QR-код"
curl -s -b "$A" "$BASE/admin/qr" > "$S"
grep -q "<svg" "$S" && ok "qr отрисован в svg" || no "нет svg с кодом"
grep -q "/q" "$S" && ok "код ведёт на /q" || no "в коде нет адреса /q"
c=$(code "$BASE/q"); [ "$c" = 307 ] && ok "/q ведёт на очередь" || no "/q вернул $c"

echo "== 16. Закрытие приёма"
c=$(code -b "$A" -X POST "$BASE/admin/session/close")
[ "$c" = 303 ] && ok "приём закрыт" || no "закрытие вернуло $c"
n=$(sql "select count(*) from queue_session where closed_at is null")
[ "$n" = 0 ] && ok "открытых приёмов не осталось" || no "открытых сеансов: $n"
curl -s "$BASE/" | grep -q "Сейчас приёма нет" && ok "студент снова видит «приёма нет»" || no "страница не обновилась"
c=$(code "$BASE/join"); [ "$c" = 303 ] && ok "после закрытия встать нельзя" || no "/join после закрытия: $c"

echo "== 17. История"
curl -s -b "$A" "$BASE/admin/sessions" | grep -q "1215" && ok "приём виден в истории" || no "истории нет"
curl -s -b "$A" "$BASE/admin/sessions/$SID" | grep -q "Иванов Иван" && ok "список приёма сохранился" || no "список потерян"
curl -s -b "$A" --get --data-urlencode "q=Иванов" "$BASE/admin/entries" | grep -q "Иванов Иван" \
  && ok "поиск по записям работает" || no "поиск не нашёл"
curl -s -b "$A" --get --data-urlencode "group=АИ-23-04" "$BASE/admin/entries" | grep -q "Петрова Анна" \
  && ok "фильтр по группе работает" || no "фильтр по группе не сработал"
n=$(sql "select count(*) from queue_entry where session_id=$SID")
[ "$n" = 4 ] && ok "все четыре записи на месте" || no "записей в истории: $n"

echo "== 18. Регрессионные проверки"
"${compose[@]}" exec -T app python -m unittest discover -s /tests -v \
  && ok "серверные регрессионные проверки" || no "серверные регрессионные проверки"
node --test "$CD/tests/ui.test.cjs" \
  && ok "клиентские регрессионные проверки" || no "клиентские регрессионные проверки"

echo
echo "Итог: $pass ок, $fail провалов"
[ "$fail" = 0 ]
