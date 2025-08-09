import telebot
from telebot import types
import sqlite3
import threading
import time
from datetime import datetime, timedelta
import logging
from dotenv import load_dotenv
import os

# Настройка логирования с временными метками
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Загрузка переменных из .env
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1002842558712"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "5161127199"))

if not TOKEN:
    raise ValueError("BOT_TOKEN не указан в .env файле")

bot = telebot.TeleBot(TOKEN)

# Подключение к SQLite с блокировкой для потокобезопасности
conn = sqlite3.connect('subscriptions.db', check_same_thread=False)
cursor = conn.cursor()
db_lock = threading.Lock()

# Создание/обновление таблицы subscriptions
def init_db():
    with db_lock:
        # Создаём таблицу, если она не существует
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY,
            expire_time INTEGER,
            payment_id TEXT
        )
        ''')
        # Проверяем, существует ли столбец notification_sent
        cursor.execute("PRAGMA table_info(subscriptions)")
        columns = [info[1] for info in cursor.fetchall()]
        if 'notification_sent' not in columns:
            cursor.execute('ALTER TABLE subscriptions ADD COLUMN notification_sent INTEGER DEFAULT 0')
        conn.commit()

# Вызываем инициализацию базы данных
init_db()

# Константы
PRICE_MONTH_STARS = 100
CURRENCY = "XTR"

# Функции для работы с базой данных
def add_or_update_subscription(user_id, expire_time, payment_id=None, notification_sent=0):
    with db_lock:
        cursor.execute('''
        INSERT INTO subscriptions (user_id, expire_time, payment_id, notification_sent)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET 
            expire_time=excluded.expire_time, 
            payment_id=excluded.payment_id,
            notification_sent=excluded.notification_sent
        ''', (user_id, expire_time, payment_id, notification_sent))
        conn.commit()

def get_subscription(user_id):
    with db_lock:
        cursor.execute('SELECT expire_time, payment_id, notification_sent FROM subscriptions WHERE user_id=?', (user_id,))
        return cursor.fetchone()

def remove_subscription(user_id):
    with db_lock:
        cursor.execute('DELETE FROM subscriptions WHERE user_id=?', (user_id,))
        conn.commit()

# Уведомление администратора о запросе на возврат
def notify_admin_refunded(user_id, payment_id, chat_id):
    try:
        bot.send_message(
            ADMIN_ID,
            f"Запрос на возврат:\nUser ID: {user_id}\nChat ID: {chat_id}\nPayment ID: {payment_id or 'Нет'}\nПожалуйста, обработайте возврат вручную."
        )
        return True
    except Exception as e:
        logging.error(f"Ошибка отправки уведомления администратору о возврате для пользователя {user_id}: {e}")
        return False

# Создание клавиатуры
def make_keyboard(user_has_sub=False, user_in_group=True):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    if user_has_sub:
        kb.row(types.KeyboardButton('Статус подписки'), types.KeyboardButton('Продлить подписку'))
        kb.row(types.KeyboardButton('Отменить подписку'))
        if not user_in_group:
            kb.row(types.KeyboardButton('Получить ссылку для входа'))
    else:
        kb.row(types.KeyboardButton('Оформить подписку'))
    return kb

def make_payment_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="Оплатить", pay=True))
    return kb

# Обработчик pre_checkout_query
@bot.pre_checkout_query_handler(func=lambda query: True)
def pre_checkout_query(pre_checkout_query):
    try:
        bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
    except Exception as e:
        logging.error(f"Ошибка pre_checkout_query: {e}")
        bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message=str(e))

@bot.message_handler(commands=['addsub'])
def add_subscription_admin(message):
    if message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, "❌ Доступ запрещён.")
        return
    
    now = datetime.now()
    expire = now + timedelta(days=30)
    add_or_update_subscription(ADMIN_ID, int(expire.timestamp()), payment_id="admin_manual_add", notification_sent=0)
    bot.send_message(message.chat.id, f"✅ Подписка для администратора добавлена до {expire.strftime('%d-%m-%Y %H:%M:%S')}")

@bot.message_handler(func=lambda m: m.text == 'Получить ссылку для входа')
def send_invite_link(message):
    user_id = message.from_user.id
    sub = get_subscription(user_id)

    # Проверяем наличие подписки и её актуальность
    if not sub or datetime.fromtimestamp(sub[0]) < datetime.now():
        bot.send_message(message.chat.id, "❌ У вас нет активной подписки. Пожалуйста, оформите подписку.")
        return

    try:
        # Проверяем статус пользователя в канале
        member = bot.get_chat_member(CHANNEL_ID, user_id)
        if member.status not in ['left', 'kicked']:
            bot.send_message(message.chat.id, "✅ Вы уже состоите в канале. Ссылка не нужна.")
            return

        # Создаём персональную ссылку с ограничением по времени (24 часа)
        expire_time = int((datetime.now() + timedelta(hours=24)).timestamp())
        invite_link = bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            member_limit=1,
            expire_date=expire_time,
            creates_join_request=False
        )

        bot.send_message(message.chat.id,
                         f"Вот ваша персональная ссылка для входа в канал (действует 24 часа, один раз):\n{invite_link.invite_link}")

    except Exception as e:
        bot.send_message(message.chat.id, "Ошибка при создании ссылки. Пожалуйста, попробуйте позже.")
        logging.error(f"Ошибка создания ссылки приглашения для пользователя {user_id}: {e}")


@bot.message_handler(commands=['start'])
def start(message):
    sub = get_subscription(message.from_user.id)
    user_in_group = False
    if sub:
        try:
            member = bot.get_chat_member(CHANNEL_ID, message.from_user.id)
            user_in_group = member.status not in ['left', 'kicked']
        except Exception:
            user_in_group = False
    kb = make_keyboard(user_has_sub=bool(sub), user_in_group=user_in_group)
    if sub:
        if user_in_group:
            bot.send_message(message.chat.id, "Привет! Управление подпиской через кнопки ниже.", reply_markup=kb)
        else:
            bot.send_message(message.chat.id, "Вы не в группе. Чтобы вернуться, получите ссылку ниже.", reply_markup=kb)
    else:
        bot.send_message(message.chat.id, "Привет! Чтобы получить доступ к каналу, оформите подписку.", reply_markup=kb)




# Обработчик команды /viewdb
@bot.message_handler(commands=['viewdb'])
def view_db(message):
    if message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, "❌ Доступ запрещён. Эта команда только для администратора.")
        return
    try:
        with db_lock:
            subs = cursor.execute('SELECT user_id, expire_time, payment_id, notification_sent FROM subscriptions').fetchall()
        if not subs:
            bot.send_message(message.chat.id, "База данных пуста.")
            return
        response = "📋 Содержимое базы данных:\n\n"
        for user_id, expire_time, payment_id, notification_sent in subs:
            expire_date = datetime.fromtimestamp(expire_time).strftime('%d-%m-%Y %H:%M:%S')
            response += f"User ID: {user_id}\nПодписка до: {expire_date}\nPayment ID: {payment_id or 'Нет'}\nУведомление отправлено: {notification_sent}\n\n"
            if len(response) > 3500:  # Лимит сообщения Telegram
                bot.send_message(message.chat.id, response)
                response = ""
        if response:
            bot.send_message(message.chat.id, response)
    except Exception as e:
        logging.error(f"Ошибка при просмотре базы данных: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}")

# Обработчик команды /refund
@bot.message_handler(commands=['refund'])
def refund(message):
    sub = get_subscription(message.from_user.id)
    if not sub or not sub[1]:
        bot.send_message(message.chat.id, "У вас нет активной подписки или данных о платеже.")
        return
    try:
        notify_admin_refunded(message.from_user.id, sub[1], message.chat.id)
        try:
            bot.ban_chat_member(CHANNEL_ID, message.from_user.id)
            bot.unban_chat_member(CHANNEL_ID, message.from_user.id)
        except Exception as e:
            logging.error(f"Ошибка при удалении пользователя {message.from_user.id} из канала: {e}")
        remove_subscription(message.from_user.id)
        kb = make_keyboard(user_has_sub=False)
        bot.send_message(
            message.chat.id,
            "✅ Запрос на возврат отправлен администратору. Подписка отменена.",
            reply_markup=kb
        )
    except Exception as e:
        logging.error(f"Ошибка при обработке возврата для пользователя {message.from_user.id}: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}")

# Обработчик кнопки "Оформить подписку"
@bot.message_handler(func=lambda m: m.text == 'Оформить подписку')
def buy_subscription(message):
    try:
        # Проверка статуса пользователя в канале
        member = bot.get_chat_member(CHANNEL_ID, message.from_user.id)
        if member.status in ['member', 'administrator', 'creator', 'restricted']:
            bot.send_message(message.chat.id, "Вы уже состоите в канале. Используйте 'Продлить подписку' для продления.")
            return
        prices = [types.LabeledPrice(label='Подписка на 30 дней', amount=PRICE_MONTH_STARS)]
        bot.send_invoice(
            chat_id=message.chat.id,
            title="Оформление подписки",
            description="Подписка на закрытый канал на 30 дней",
            invoice_payload="subscribe_month",
            provider_token="",  # Telegram Stars не требует provider_token
            currency=CURRENCY,
            prices=prices,
            start_parameter="subscribe-month",
            reply_markup=make_payment_keyboard()
        )
    except Exception as e:
        logging.error(f"Ошибка отправки инвойса для {message.from_user.id}: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}")

# Обработчик кнопки "Продлить подписку"
@bot.message_handler(func=lambda m: m.text == 'Продлить подписку')
def extend_subscription(message):
    sub = get_subscription(message.from_user.id)
    if not sub:
        bot.send_message(message.chat.id, "У вас нет активной подписки. Нажмите 'Оформить подписку' для начала.")
        return
    prices = [types.LabeledPrice(label='Продление подписки на 30 дней', amount=PRICE_MONTH_STARS)]
    try:
        bot.send_invoice(
            chat_id=message.chat.id,
            title="Продление подписки",
            description="Продление подписки на закрытый канал на 30 дней",
            invoice_payload="extend_month",
            provider_token="",
            currency=CURRENCY,
            prices=prices,
            start_parameter="extend-month",
            reply_markup=make_payment_keyboard()
        )
    except Exception as e:
        logging.error(f"Ошибка отправки инвойса для {message.from_user.id}: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}")

# Обработчик кнопки "Отменить подписку"
@bot.message_handler(func=lambda m: m.text == 'Отменить подписку')
def cancel_subscription(message):
    sub = get_subscription(message.from_user.id)
    if not sub:
        bot.send_message(message.chat.id, "У вас нет активной подписки.")
        return
    try:
        try:
            bot.ban_chat_member(CHANNEL_ID, message.from_user.id)
            bot.unban_chat_member(CHANNEL_ID, message.from_user.id)
        except Exception as e:
            logging.error(f"Ошибка при удалении пользователя {message.from_user.id} из канала: {e}")
        remove_subscription(message.from_user.id)
        kb = make_keyboard(user_has_sub=False)
        bot.send_message(message.chat.id, "✅ Ваша подписка отменена, доступ к каналу закрыт.", reply_markup=kb)
    except Exception as e:
        logging.error(f"Ошибка при отмене подписки для {message.from_user.id}: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка при отмене: {e}")

# Обработчик кнопки "Статус подписки"
@bot.message_handler(func=lambda m: m.text == 'Статус подписки')
def status_subscription(message):
    sub = get_subscription(message.from_user.id)
    if not sub:
        kb = make_keyboard(user_has_sub=False)
        bot.send_message(message.chat.id, "У вас нет активной подписки.", reply_markup=kb)
        return
    expire_time = datetime.fromtimestamp(sub[0])
    remaining = expire_time - datetime.now()
    if remaining.total_seconds() <= 0:
        kb = make_keyboard(user_has_sub=False)
        bot.send_message(message.chat.id, "Ваша подписка уже закончилась.", reply_markup=kb)
        return
    days = remaining.days
    hours, remainder = divmod(remaining.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    kb = make_keyboard(user_has_sub=True)
    bot.send_message(
        message.chat.id,
        f"Ваша подписка действует ещё: {days} дн {hours} ч {minutes} мин.",
        reply_markup=kb
    )


@bot.chat_member_handler()
def handle_new_member(update):
    user = update.new_chat_member.user
    status = update.new_chat_member.status
    user_id = user.id

    # Проверяем, что пользователь присоединился (статус стал member или restricted)
    if status in ['member', 'restricted']:
        # Проверяем подписку в базе
        with db_lock:
            sub = cursor.execute('SELECT expire_time FROM subscriptions WHERE user_id = ?', (user_id,)).fetchone()

        if not sub:
            # Нет подписки — баним
            try:
                bot.ban_chat_member(CHANNEL_ID, user_id)
                bot.unban_chat_member(CHANNEL_ID, user_id)  # Чтобы пользователь не мог вернуться без ссылки
                bot.send_message(user_id, "❌ У вас нет активной подписки, доступ к каналу закрыт.")
            except Exception as e:
                logging.error(f"Ошибка при бане пользователя без подписки {user_id}: {e}")
        else:
            expire_time = datetime.fromtimestamp(sub[0])
            if expire_time < datetime.now():
                # Подписка истекла — тоже баним
                try:
                    bot.ban_chat_member(CHANNEL_ID, user_id)
                    bot.unban_chat_member(CHANNEL_ID, user_id)
                    bot.send_message(user_id, "⏰ Ваша подписка истекла, доступ к каналу закрыт.")
                    with db_lock:
                        cursor.execute('DELETE FROM subscriptions WHERE user_id = ?', (user_id,))
                        conn.commit()
                except Exception as e:
                    logging.error(f"Ошибка при бане пользователя с истекшей подпиской {user_id}: {e}")
                    
# Обработчик успешного платежа
@bot.message_handler(content_types=['successful_payment'])
def got_payment(message):
    try:
        payload = message.successful_payment.invoice_payload
        user_id = message.from_user.id
        payment_id = message.successful_payment.telegram_payment_charge_id
        now = datetime.now()
        sub = get_subscription(user_id)
        new_expire = now + timedelta(days=30) if not sub or datetime.fromtimestamp(sub[0]) < now else datetime.fromtimestamp(sub[0]) + timedelta(days=30)
        add_or_update_subscription(user_id, int(new_expire.timestamp()), payment_id, notification_sent=0)
        kb = make_keyboard(user_has_sub=True)
        bot.send_message(
            message.chat.id,
            f"✅ Оплата получена! Подписка активна до {new_expire.strftime('%d-%m-%Y %H:%M:%S')}",
            reply_markup=kb
        )
        try:
            member = bot.get_chat_member(CHANNEL_ID, user_id)
            if member.status in ['left', 'kicked']:
                invite_link = bot.create_chat_invite_link(CHANNEL_ID, member_limit=1)
                bot.send_message(user_id, f"Присоединяйтесь к каналу: {invite_link.invite_link}")
            else:
                bot.send_message(user_id, "Вы уже в канале. Доступ продлён!")
        except Exception as e:
            logging.error(f"Ошибка проверки/добавления пользователя {user_id} в канал: {e}")
            invite_link = bot.create_chat_invite_link(CHANNEL_ID, member_limit=1)
            bot.send_message(user_id, f"Присоединяйтесь к каналу: {invite_link.invite_link}")
    except Exception as e:
        logging.error(f"Ошибка обработки платежа для {user_id}: {e}")
        bot.send_message(message.chat.id, f"❌ Ошибка обработки платежа: {e}")

# Фоновый процесс проверки подписок
def subscription_watcher():
    while True:
        now_ts = int(datetime.now().timestamp())
        with db_lock:
            subs = cursor.execute('SELECT user_id, expire_time, notification_sent FROM subscriptions').fetchall()
        expired_count = 0
        for user_id, expire_time, notification_sent in subs:
            remain = expire_time - now_ts
            if remain <= 0:
                try:
                    member = bot.get_chat_member(CHANNEL_ID, user_id)
                    if member.status not in ['left', 'kicked']:
                        bot.send_message(user_id, "⏰ Ваша подписка закончилась. Вы удалены из канала.")
                        bot.ban_chat_member(CHANNEL_ID, user_id)
                        bot.unban_chat_member(CHANNEL_ID, user_id)
                        expired_count += 1
                    remove_subscription(user_id)
                except Exception as e:
                    logging.error(f"Ошибка при удалении пользователя {user_id}: {e}")
            elif 0 < remain <= 24 * 3600 and not notification_sent:
                try:
                    bot.send_message(
                        user_id,
                        "⏰ Ваша подписка истекает через сутки! Продлите её с помощью кнопки 'Продлить подписку'."
                    )
                    add_or_update_subscription(user_id, expire_time, None, notification_sent=1)
                except Exception as e:
                    logging.error(f"Ошибка отправки уведомления пользователю {user_id}: {e}")
        logging.info(f"Проверено подписок: {len(subs)}, удалено истекших: {expired_count}")
        time.sleep(60)

# Запуск фонового процесса
threading.Thread(target=subscription_watcher, daemon=True).start()

if __name__ == "__main__":
    print("Бот запущен...")
    try:
        # Отключаем вебхук перед запуском polling
        bot.delete_webhook(drop_pending_updates=True)
        bot.polling(none_stop=True)
    except Exception as e:
        logging.error(f"Ошибка в polling: {e}")
        time.sleep(10)  # Задержка перед перезапуском
        bot.delete_webhook(drop_pending_updates=True)
        bot.polling(none_stop=True)