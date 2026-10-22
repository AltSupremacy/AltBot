import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

logging.basicConfig(level=logging.INFO)

# ---- Настройки (задаются переменными окружения на хостинге) ----
BOT_TOKEN = os.environ["BOT_TOKEN"]
CRYPTO_TOKEN = os.environ["CRYPTO_TOKEN"]        # токен из @CryptoBot -> Crypto Pay -> Create App
CHANNEL_ID = int(os.environ["CHANNEL_ID"])       # например -1001234567890
PRICE_STARS = int(os.getenv("PRICE_STARS", "500"))
PRICE_USDT = os.getenv("PRICE_USDT", "10")
DAYS = int(os.getenv("DAYS", "30"))
DB_PATH = os.getenv("DB_PATH", "data.db")
TITLE = os.getenv("TITLE", "Доступ в приватный канал")

CP_API = "https://pay.crypt.bot/api/"

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("create table if not exists subs(user_id integer primary key, expires integer)")
db.execute(
    "create table if not exists invoices("
    "invoice_id integer primary key, user_id integer, done integer default 0)"
)
db.commit()

router = Router()


async def cp(method: str, **params):
    async with aiohttp.ClientSession() as s:
        async with s.post(
            CP_API + method,
            headers={"Crypto-Pay-API-Token": CRYPTO_TOKEN},
            json=params,
        ) as r:
            data = await r.json()
    if not data.get("ok"):
        raise RuntimeError(data)
    return data["result"]


async def grant(bot: Bot, user_id: int):
    now = int(time.time())
    row = db.execute("select expires from subs where user_id=?", (user_id,)).fetchone()
    base = max(row[0], now) if row else now
    exp = base + DAYS * 86400
    db.execute(
        "insert into subs(user_id, expires) values(?, ?) "
        "on conflict(user_id) do update set expires=excluded.expires",
        (user_id, exp),
    )
    db.commit()
    link = await bot.create_chat_invite_link(
        CHANNEL_ID, member_limit=1, expire_date=now + 3600
    )
    until = datetime.fromtimestamp(exp).strftime("%d.%m.%Y")
    await bot.send_message(
        user_id,
        f"✅ Оплата получена! Доступ активен до {until}.\n\n"
        f"Ваша ссылка (одноразовая, действует 1 час):\n{link.invite_link}",
    )


@router.message(CommandStart())
async def start(m: Message):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"⭐ {PRICE_STARS} Stars", callback_data="pay_stars")],
            [InlineKeyboardButton(text=f"💲 {PRICE_USDT} USDT (CryptoBot)", callback_data="pay_crypto")],
        ]
    )
    await m.answer(f"{TITLE}\nСрок: {DAYS} дн.\n\nВыберите способ оплаты:", reply_markup=kb)


@router.callback_query(F.data == "pay_stars")
async def pay_stars(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer_invoice(
        title=TITLE,
        description=f"Доступ на {DAYS} дн.",
        payload="sub",
        currency="XTR",
        prices=[LabeledPrice(label="Доступ", amount=PRICE_STARS)],
    )


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)


@router.message(F.successful_payment)
async def paid_stars(m: Message, bot: Bot):
    await grant(bot, m.from_user.id)


@router.callback_query(F.data == "pay_crypto")
async def pay_crypto(cb: CallbackQuery):
    await cb.answer()
    inv = await cp(
        "createInvoice",
        asset="USDT",
        amount=PRICE_USDT,
        description=f"{TITLE} ({DAYS} дн.)",
        expires_in=3600,
    )
    db.execute(
        "insert into invoices(invoice_id, user_id) values(?, ?)",
        (inv["invoice_id"], cb.from_user.id),
    )
    db.commit()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Оплатить", url=inv["bot_invoice_url"])]]
    )
    await cb.message.answer(
        "Счёт создан. После оплаты ссылка придёт сюда автоматически (до минуты).",
        reply_markup=kb,
    )


async def poll_crypto(bot: Bot):
    while True:
        try:
            rows = db.execute("select invoice_id, user_id from invoices where done=0").fetchall()
            if rows:
                res = await cp("getInvoices", invoice_ids=",".join(str(r[0]) for r in rows))
                items = res["items"] if isinstance(res, dict) else res
                status = {i["invoice_id"]: i["status"] for i in items}
                for iid, uid in rows:
                    st = status.get(iid)
                    if st == "paid":
                        db.execute("update invoices set done=1 where invoice_id=?", (iid,))
                        db.commit()
                        await grant(bot, uid)
                    elif st == "expired":
                        db.execute("update invoices set done=2 where invoice_id=?", (iid,))
                        db.commit()
        except Exception:
            logging.exception("poll_crypto")
        await asyncio.sleep(15)


async def kick_expired(bot: Bot):
    while True:
        try:
            now = int(time.time())
            for (uid,) in db.execute("select user_id from subs where expires < ?", (now,)).fetchall():
                try:
                    await bot.ban_chat_member(CHANNEL_ID, uid)
                    await bot.unban_chat_member(CHANNEL_ID, uid, only_if_banned=True)
                except Exception:
                    logging.exception("kick %s", uid)
                db.execute("delete from subs where user_id=?", (uid,))
                db.commit()
                try:
                    await bot.send_message(uid, "⏰ Подписка закончилась. Чтобы продлить, нажмите /start")
                except Exception:
                    pass
        except Exception:
            logging.exception("kick_expired")
        await asyncio.sleep(600)


async def main():
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    tasks = [asyncio.create_task(poll_crypto(bot)), asyncio.create_task(kick_expired(bot))]
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()


if __name__ == "__main__":
    asyncio.run(main())
