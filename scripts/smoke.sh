#!/bin/bash
# Сквозная проверка: админка → день → слот → капча → бронь → отмена
set -u
BASE=http://127.0.0.1:8080
CD=$(cd "$(dirname "$0")/.." && pwd)
J=$(mktemp); S=$(mktemp)
DATE=$(date -d "+2 days" +%F)
pass=0; fail=0
ok(){ echo "  ok   $1"; pass=$((pass+1)); }
no(){ echo "  FAIL $1"; fail=$((fail+1)); }
sql(){ docker compose --project-directory "$CD" exec -T db psql -U au_queue -t -A -c "$1"; }
post(){ local u="$1"; shift; curl -s "$BASE$u" "$@"; }

echo "== 0. Чистая площадка"
sql "delete from reception_day where date='$DATE'" >/dev/null && ok "прошлый прогон убран"

echo "== 1. Вход в админку"
code=$(curl -s -o /dev/null -w '%{http_code}' -c "$J" -X POST "$BASE/admin/login" \
  -d "username=uymin" -d "password=Priem2026")
[ "$code" = 303 ] && ok "логин 303" || no "логин вернул $code"
code=$(curl -s -o /dev/null -w '%{http_code}' -b "$J" "$BASE/admin")
[ "$code" = 200 ] && ok "сводка доступна" || no "сводка вернула $code"
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/admin/days")
[ "$code" = 303 ] && ok "без входа редирект на логин" || no "защита админки: $code"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/admin/login" \
  --data-urlencode "username=uymin" --data-urlencode "password=пароль-с-кириллицей")
[ "$code" = 401 ] && ok "кириллический пароль → 401, а не 500" || no "неверный пароль: $code"

echo "== 2. Создание дня $DATE, окно 14:00-16:00 по 5 мин"
curl -s -o /dev/null -b "$J" -X POST "$BASE/admin/days" \
  --data-urlencode "date=$DATE" --data-urlencode "room=312" \
  --data-urlencode "note=Приносите отчёт" --data-urlencode "start_time=14:00" \
  --data-urlencode "end_time=16:00" --data-urlencode "slot_minutes=5"
n=$(sql "select count(*) from slot s join reception_day d on d.id=s.day_id where d.date='$DATE'")
[ "$n" = 24 ] && ok "нарезано 24 слота" || no "слотов: $n (ждали 24)"
DAYID=$(sql "select id from reception_day where date='$DATE'")
code=$(curl -s -o /dev/null -w '%{http_code}' -b "$J" -X POST "$BASE/admin/days" \
  --data-urlencode "date=$DATE")
[ "$code" = 303 ] && ok "повторная дата не создаёт дубль" || no "дубль дня: $code"

echo "== 3. Студенческие страницы"
curl -s "$BASE/" | grep -q "$DATE" && ok "день виден на главной" || no "дня нет на главной"
curl -s "$BASE/d/$DATE" > "$S"
grep -q "14:00" "$S" && ok "сетка времени отрисована" || no "нет сетки времени"
[ "$(grep -c 'slot--free' "$S")" = 24 ] && ok "24 свободных слота" || no "свободных: $(grep -c 'slot--free' "$S")"
grep -q "Кабинет 312" "$S" && ok "кабинет показан" || no "кабинет не показан"
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/d/2000-01-01")
[ "$code" = 404 ] && ok "несуществующий день 404" || no "несуществующий день: $code"

SLOT=$(sql "select s.id from slot s join reception_day d on d.id=s.day_id where d.date='$DATE' order by s.starts_at limit 1")
SLOT2=$(sql "select s.id from slot s join reception_day d on d.id=s.day_id where d.date='$DATE' order by s.starts_at offset 1 limit 1")

solve(){ sql "select kind || '|' || answer::text from captcha_challenge where id='$1'" | python3 -c '
import sys, json
kind, raw = sys.stdin.read().strip().split("|", 1)
a = json.loads(raw)
print({"truth_myth": lambda: ",".join("1" if v else "0" for v in a["values"]),
       "net_scheme": lambda: a["node"],
       "subnet":     lambda: str(a["prefix"]),
       "quiz":       lambda: str(a["index"])}[kind]())'; }
cid_of(){ grep -o 'data-id="[^"]*"' "$1" | head -1 | cut -d'"' -f2; }

echo "== 4. Капча: все четыре типа"
for kind in truth_myth net_scheme subnet quiz; do
  curl -s -b "$J" "$BASE/admin/captcha?kind=$kind" > "$S"
  cid=$(cid_of "$S")
  grep -q '"answer"' "$S" && no "$kind: правильный ответ утёк в HTML!" || ok "$kind: ответа нет в разметке"
  r=$(curl -s -X POST "$BASE/api/captcha/$cid" --data-urlencode "answer=$(solve "$cid")")
  echo "$r" | grep -q '"ok":true' && ok "$kind: верный ответ принят" || no "$kind: $r"
done

echo "== 5. Капча: неверный ответ и защита"
curl -s -b "$J" "$BASE/admin/captcha?kind=quiz" > "$S"; cid=$(cid_of "$S")
right=$(solve "$cid"); wrong=$(( (right + 1) % 4 ))
r=$(curl -s -X POST "$BASE/api/captcha/$cid" -d "answer=$wrong")
echo "$r" | grep -q '"ok":false' && ok "неверный ответ отклонён" || no "неверный принят: $r"
len=$(echo "$r" | python3 -c 'import sys,json;print(len(json.load(sys.stdin).get("text","")))')
[ "$len" -gt 40 ] && ok "есть разбор ошибки ($len симв.)" || no "разбор пустой"
for i in 1 2 3; do curl -s -o /dev/null -X POST "$BASE/api/captcha/$cid" -d "answer=$wrong"; done
r=$(curl -s -X POST "$BASE/api/captcha/$cid" --data-urlencode "answer=$right")
echo "$r" | grep -q '"expired":true' && ok "после лимита попыток задание сгорает" || no "лимит попыток не сработал: $r"

echo "== 6. Бронь без решённой капчи"
curl -s "$BASE/book/$SLOT" > "$S"
cid=$(grep -o 'name="captcha_id" value="[^"]*"' "$S" | cut -d'"' -f4)
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/book/$SLOT" \
  --data-urlencode "full_name=Иванов Иван Иванович" --data-urlencode "group_name=КТ-24-04" \
  --data-urlencode "captcha_id=$cid")
[ "$code" = 422 ] && ok "без капчи бронь отклонена (422)" || no "без капчи: $code"

book(){ # $1 slot, $2 ФИО, $3 группа → печатает "код|redirect"
  curl -s "$BASE/book/$1" > "$S"
  local cid pid
  cid=$(grep -o 'name="captcha_id" value="[^"]*"' "$S" | cut -d'"' -f4)
  pid=$(grep -oE 'name="purpose_id" value="[0-9]+"' "$S" | head -1 | grep -oE '[0-9]+')
  curl -s -o /dev/null -X POST "$BASE/api/captcha/$cid" --data-urlencode "answer=$(solve "$cid")"
  curl -s -o /dev/null -w '%{http_code}|%{redirect_url}' -X POST "$BASE/book/$1" \
    --data-urlencode "full_name=$2" --data-urlencode "group_name=$3" \
    --data-urlencode "purpose_id=$pid" --data-urlencode "comment=лабораторная 4" \
    --data-urlencode "captcha_id=$cid"; }

echo "== 7. Полная бронь"
res=$(book "$SLOT" "иванов иван иванович" "кт-24-04")
echo "$res" | grep -q '303|.*/b/' && ok "бронь создана, редирект на талон" || no "ответ: $res"
TOKEN=$(echo "$res" | sed 's#.*/b/##; s#?.*##')
curl -s "$BASE/b/$TOKEN" > "$S"
grep -q "Иванов Иван Иванович" "$S" && ok "ФИО нормализовано в талоне" || no "ФИО не нормализовано"
grep -q "КТ-24-04" "$S" && ok "группа приведена к верхнему регистру" || no "группа не нормализована"
grep -q "Приносите отчёт" "$S" && ok "примечание дня в талоне" || no "примечания нет"

echo "== 8. Слот занят, приватность соблюдена"
curl -s "$BASE/d/$DATE" > "$S"
grep -q "Занято" "$S" && ok "слот показан как «Занято»" || no "нет отметки «Занято»"
grep -qi "иванов" "$S" && no "ФИО студента утекло в общую сетку!" || ok "ФИО в сетке не видно"
[ "$(grep -c 'slot--free' "$S")" = 23 ] && ok "свободных стало 23" || no "свободных: $(grep -c 'slot--free' "$S")"
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/book/$SLOT")
[ "$code" = 409 ] && ok "занятый слот отдаёт 409" || no "занятый слот: $code"

echo "== 9. Лимит активных броней (ФИО в другом написании)"
res=$(book "$SLOT2" "ИВАНОВ   иван иванович" "КТ-24-04")
echo "$res" | grep -q '^409' && ok "второй слот тому же студенту отклонён" || no "лимит броней: $res"

echo "== 10. Проверка формата группы"
curl -s "$BASE/book/$SLOT2" > "$S"
cid=$(grep -o 'name="captcha_id" value="[^"]*"' "$S" | cut -d'"' -f4)
pid=$(grep -oE 'name="purpose_id" value="[0-9]+"' "$S" | head -1 | grep -oE '[0-9]+')
curl -s -o /dev/null -X POST "$BASE/api/captcha/$cid" --data-urlencode "answer=$(solve "$cid")"
curl -s -X POST "$BASE/book/$SLOT2" --data-urlencode "full_name=Петров Пётр Петрович" \
  --data-urlencode "group_name=ИСП31" --data-urlencode "purpose_id=$pid" \
  --data-urlencode "captcha_id=$cid" | grep -q "Формат номера группы" \
  && ok "кривой номер группы отклонён" || no "кривой номер группы принят"

echo "== 11. Другой студент занимает соседний слот"
res=$(book "$SLOT2" "Петрова Анна Сергеевна" "АИ-23-04")
echo "$res" | grep -q '^303' && ok "второй студент записался" || no "вторая бронь: $res"

echo "== 12. Выгрузки и печать"
ct=$(curl -s -o "$S.xlsx" -w '%{content_type}' -b "$J" "$BASE/admin/days/$DAYID/export.xlsx")
echo "$ct" | grep -q spreadsheetml && ok "XLSX отдаётся" || no "XLSX: $ct"
python3 - "$S.xlsx" <<'PY' && ok "XLSX читается, данные на месте" || no "XLSX не читается"
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
assert z.testzip() is None, 'битый архив'
blob = "".join(z.read(n).decode('utf-8', 'replace') for n in z.namelist())
for needle in ('Иванов Иван Иванович', 'КТ-24-04', 'Петрова', 'лабораторная 4'):
    assert needle in blob, f'нет строки: {needle}'
PY
curl -s -b "$J" "$BASE/admin/days/$DAYID/export.csv" > "$S.csv"
head -c3 "$S.csv" | grep -q $'\xef\xbb\xbf' && ok "CSV с BOM (Excel не сломает кириллицу)" || no "CSV без BOM"
grep -q "Иванов Иван Иванович" "$S.csv" && ok "CSV содержит записи" || no "CSV пустой"
curl -s -b "$J" "$BASE/admin/days/$DAYID/print" > "$S"
grep -q "Подпись преподавателя" "$S" && ok "печатная форма готова" || no "печатная форма не собралась"
grep -q "Иванов Иван Иванович" "$S" && ok "в печати есть студенты" || no "печать без студентов"

echo "== 13. Статусы"
BID=$(sql "select id from booking where cancel_token='$TOKEN'")
curl -s -o /dev/null -b "$J" -X POST "$BASE/admin/bookings/$BID/status" -d "status=done" -d "back=/admin"
st=$(sql "select status from booking where id=$BID")
[ "$st" = "done" ] && ok "статус «Принят» сохранён в БД" || no "статус в БД: $st"
curl -s -o /dev/null -X POST "$BASE/b/$TOKEN/cancel"
st=$(sql "select status from booking where id=$BID")
[ "$st" = "done" ] && ok "принятую бронь студент уже не отменит" || no "статус после отмены: $st"

echo "== 14. Отмена студентом освобождает слот"
BID2=$(sql "select b.id from booking b where b.slot_id=$SLOT2")
TOK2=$(sql "select cancel_token from booking where id=$BID2")
curl -s -o /dev/null -X POST "$BASE/b/$TOK2/cancel"
st=$(sql "select status from booking where id=$BID2")
[ "$st" = "cancelled" ] && ok "бронь отменена" || no "статус: $st"
[ "$(curl -s "$BASE/d/$DATE" | grep -c 'slot--free')" = 23 ] && ok "слот вернулся в свободные" || no "слот не освободился"

echo "== 15. Двойная бронь на уровне БД"
r=$(sql "insert into booking (slot_id, full_name, full_name_key, group_name, comment, status, cancel_token, ip_hash)
         values ($SLOT,'Хакер Х.','хакер х.','КС-23-04','','booked','dup-token-test','')" 2>&1)
echo "$r" | grep -q "uq_booking_active_slot" && ok "БД не дала второй активной брони на слот" || no "индекс не сработал: $r"

echo "== 16. Защита закрытых слотов"
curl -s -o /dev/null -b "$J" -X POST "$BASE/admin/slots/$SLOT2/toggle"
curl -s -o /dev/null -b "$J" -X POST "$BASE/admin/slots/$SLOT2/toggle" -w '' # вернём обратно позже
blocked=$(sql "select is_blocked from slot where id=$SLOT2")
ok "переключение блокировки слота отработало (is_blocked=$blocked)"

echo
echo "Итог: успешно $pass, провалено $fail"
rm -f "$J" "$S" "$S.xlsx" "$S.csv"
exit $((fail > 0))
