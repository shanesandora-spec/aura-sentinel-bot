import disnake
from disnake.ext import commands
import asyncpg
import random
import asyncio
import io
import time
import os
from datetime import datetime
from dotenv import load_dotenv

# --- [ НАСТРОЙКИ ] ---
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# Загружаем ID ролей из переменных окружения
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", 0))
DEV_ROLE_ID = int(os.getenv("DEV_ROLE_ID", 0))
MOD_ROLES_IDS = [int(x) for x in os.getenv("MOD_ROLES_IDS", "").split(",") if x]
TICKET_CATEGORY_NAME = os.getenv("TICKET_CATEGORY_NAME", "Tickets")
LOG_CHANNEL_NAME = os.getenv("LOG_CHANNEL_NAME", "logs-aura")
CURRENCY_NAME = "Aura Credits"
CURR_SYMBOL = "AC"

intents = disnake.Intents.all()
bot = commands.InteractionBot(intents=intents)

# --- [ БАЗА ДАННЫХ NEON ] ---
async def init_db():
    bot.pool = await asyncpg.create_pool(DATABASE_URL)
    async with bot.pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id BIGINT PRIMARY KEY, 
                balance INTEGER DEFAULT 0, 
                bank INTEGER DEFAULT 0, 
                last_reward INTEGER DEFAULT 0, 
                donated INTEGER DEFAULT 0, 
                last_interest INTEGER DEFAULT 0
            )
        ''')

async def get_data(user_id):
    async with bot.pool.acquire() as conn:
        res = await conn.fetchrow("SELECT balance, bank, last_reward, donated, last_interest FROM users WHERE id = $1", user_id)
        if res: return list(res)
        now = int(time.time())
        await conn.execute("INSERT INTO users (id, balance, bank, last_reward, donated, last_interest) VALUES ($1, 0, 0, 0, 0, $2)", user_id, now)
        return [0, 0, 0, 0, now]

async def update_db(user_id, amount, mode="balance"):
    await get_data(user_id)
    async with bot.pool.acquire() as conn:
        if mode == "balance": await conn.execute("UPDATE users SET balance = balance + $1 WHERE id = $2", amount, user_id)
        elif mode == "bank": await conn.execute("UPDATE users SET bank = bank + $1 WHERE id = $2", amount, user_id)
        elif mode == "reward": await conn.execute("UPDATE users SET last_reward = $1 WHERE id = $2", amount, user_id)
        elif mode == "donate": await conn.execute("UPDATE users SET donated = donated + $1 WHERE id = $2", amount, user_id)
        elif mode == "interest_time": await conn.execute("UPDATE users SET last_interest = $1 WHERE id = $2", amount, user_id)

# --- [ СИСТЕМА ЛОГИРОВАНИЯ ] ---
async def send_log(guild, title, desc, color=disnake.Color.blue(), file=None):
    log_ch = disnake.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
    if log_ch:
        emb = disnake.Embed(title=f"📜 {title}", description=desc, color=color, timestamp=datetime.now())
        if file: await log_ch.send(embed=emb, file=file)
        else: await log_ch.send(embed=emb)

async def log_ticket_final(channel, closer, opener, t_type):
    msgs = []
    async for m in channel.history(limit=1000, oldest_first=True):
        msgs.append(f"[{m.created_at.strftime('%H:%M')}] {m.author}: {m.content}")
    log_text = f"ОТЧЕТ AURA SENTINEL\nТип: {t_type}\nКлиент: {opener}\nЗакрыл: {closer}\n\n" + "\n".join(msgs)
    file = disnake.File(fp=io.BytesIO(log_text.encode('utf-8')), filename=f"log-{channel.name}.txt")
    await send_log(channel.guild, "Тикет закрыт", f"Тип: **{t_type}**\nОткрыл: {opener.mention}\nЗакрыл: {closer.mention}", color=0xe74c3c, file=file)

# --- [ ИГРОВАЯ ЛОГИКА (Блэкджек) ] ---
def get_card(): return random.choice([2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 10, 10, 11])

class BlackjackView(disnake.ui.View):
    def __init__(self, inter, bet):
        super().__init__(timeout=60)
        self.inter, self.bet = inter, bet
        self.p_cards, self.d_cards = [get_card(), get_card()], [get_card(), get_card()]

    def get_score(self, hand):
        s = sum(hand)
        while s > 21 and 11 in hand: hand[hand.index(11)] = 1; s = sum(hand)
        return s

    async def make_emb(self, status="playing"):
        p_s, d_s = self.get_score(self.p_cards), self.get_score(self.d_cards)
        color = 0x3498db if status == "playing" else (0x2ecc71 if status == "win" else (0xe74c3c if status == "lose" else 0x95a5a6))
        emb = disnake.Embed(title="🃏 Казино Aura Sentinel", color=color)
        emb.add_field(name="👤 Ваша рука", value=f"Карты: `{self.p_cards}`\nСчет: **{p_s}**", inline=True)
        d_val = f"Карты: `[{self.d_cards[0]}, ?]`" if status == "playing" else f"Карты: `{self.d_cards}`\nСчет: **{d_s}**"
        emb.add_field(name="🕵️ Дилер", value=d_val, inline=True)
        return emb

    @disnake.ui.button(label="Еще", style=disnake.ButtonStyle.green, emoji="➕")
    async def hit(self, b, i):
        if i.author.id != self.inter.author.id: return
        self.p_cards.append(get_card())
        if self.get_score(self.p_cards) > 21:
            await update_db(self.inter.author.id, -self.bet)
            await i.response.edit_message(embed=await self.make_emb("lose"), content=f"💥 **Перебор!** Ты потерял **{self.bet}** {CURR_SYMBOL}", view=None)
        else: await i.response.edit_message(embed=await self.make_emb())

    @disnake.ui.button(label="Стоп", style=disnake.ButtonStyle.red, emoji="✋")
    async def stand(self, b, i):
        if i.author.id != self.inter.author.id: return
        d_s = self.get_score(self.d_cards)
        while d_s < 17: self.d_cards.append(get_card()); d_s = self.get_score(self.d_cards)
        p_s = self.get_score(self.p_cards)
        if d_s > 21 or p_s > d_s:
            win = int((self.bet * 2) * 0.95); await update_db(self.inter.author.id, win - self.bet)
            msg, st = f"🏆 **Победа!** +{win} {CURR_SYMBOL} (с учетом ком. 5%)", "win"
        elif p_s == d_s: msg, st = "🤝 **Ничья!** Ставка возвращена.", "draw"
        else: await update_db(self.inter.author.id, -self.bet); msg, st = f"💀 **Дилер выиграл.** -{self.bet} {CURR_SYMBOL}", "lose"
        await i.response.edit_message(embed=await self.make_emb(st), content=msg, view=None)

# --- [ КОМАНДЫ ЭКОНОМИКИ ] ---

@bot.slash_command(name="balance", description="💳 Проверить баланс, банк и накопленную благотворительность")
async def balance(inter):
    data = await get_data(inter.author.id)
    bal, bank, _, don, last_int = data
    now = int(time.time())
    days = (now - last_int) // 86400
    if days >= 1 and bank > 0:
        interest = int(bank * 0.02 * days)
        await update_db(inter.author.id, interest, "bank"); await update_db(inter.author.id, now, "interest_time")
        bank += interest
        f = f"📈 Зачислено процентов: +{interest} {CURR_SYMBOL}"
    else: f = "💡 Банк начисляет 2% прибыли каждые 24 часа."
    
    emb = disnake.Embed(title=f"🏦 Счёт: {inter.author.name}", color=0x3498db)
    emb.add_field(name="💵 Кошелек", value=f"`{bal}` {CURR_SYMBOL}", inline=True)
    emb.add_field(name="🏛️ Сбережения", value=f"`{bank}` {CURR_SYMBOL}", inline=True)
    emb.add_field(name="❤️ Благотворительность", value=f"`{don}` {CURR_SYMBOL}", inline=True)
    emb.set_footer(text=f)
    await inter.send(embed=emb)

@bot.slash_command(name="deposit", description="📥 Положить Aura Credits в банк под 2% в сутки")
async def deposit(inter, amount: int):
    data = await get_data(inter.author.id)
    if amount <= 0 or data[0] < amount: return await inter.send("❌ У вас недостаточно средств в кошельке!", ephemeral=True)
    await update_db(inter.author.id, -amount); await update_db(inter.author.id, amount, "bank")
    await inter.send(f"📥 Вы внесли **{amount}** {CURR_SYMBOL} на банковский счет.")

@bot.slash_command(name="withdraw", description="📤 Снять Aura Credits с банковского счета")
async def withdraw(inter, amount: int):
    data = await get_data(inter.author.id)
    if amount <= 0 or data[1] < amount: return await inter.send("❌ В банке нет такой суммы!", ephemeral=True)
    await update_db(inter.author.id, amount); await update_db(inter.author.id, -amount, "bank")
    await inter.send(f"📤 Вы сняли **{amount}** {CURR_SYMBOL} со счета.")

# --- [ ТОПЫ ] ---

@bot.slash_command(name="top", description="🏆 Топ-10 самых богатых игроков сервера")
async def top(inter):
    async with bot.pool.acquire() as conn:
        data = await conn.fetch("SELECT id, (balance + bank) as t FROM users ORDER BY t DESC LIMIT 10")
    emb = disnake.Embed(title="🏆 Список Форбс: Aura Sentinel", color=0xf1c40f)
    for i, row in enumerate(data, 1):
        m = inter.guild.get_member(row['id'])
        name = m.display_name if m else f"ID: {row['id']}"
        emb.add_field(name=f"{i}. {name}", value=f"💰 {row['t']} {CURR_SYMBOL}", inline=False)
    await inter.send(embed=emb)

@bot.slash_command(name="top_donators", description="❤️ Топ-10 величайших меценатов сервера")
async def top_donators(inter):
    async with bot.pool.acquire() as conn:
        data = await conn.fetch("SELECT id, donated FROM users WHERE donated > 0 ORDER BY donated DESC LIMIT 10")
    if not data: return await inter.send("Меценатов пока нет. Стань первым!")
    emb = disnake.Embed(title="❤️ Доска Почета Меценатов", color=0x9b59b6)
    for i, row in enumerate(data, 1):
        m = inter.guild.get_member(row['id'])
        name = m.display_name if m else f"ID: {row['id']}"
        emb.add_field(name=f"{i}. {name}", value=f"Отдал: {row['donated']} {CURR_SYMBOL}", inline=False)
    await inter.send(embed=emb)

# --- [ АДМИН-КОМАНДА ] ---

@bot.slash_command(name="add_credits", description="💎 Выдать валюту пользователю (Только для Руководства)")
async def add_credits(inter, member: disnake.Member, amount: int):
    if not any(inter.author.get_role(rid) for rid in [ADMIN_ROLE_ID, DEV_ROLE_ID]):
        return await inter.send("❌ У вас нет прав для использования этой команды!", ephemeral=True)
    await update_db(member.id, amount)
    await inter.send(f"💎 Администратор {inter.author.mention} выдал **{amount}** {CURR_SYMBOL} игроку {member.mention}")
    await send_log(inter.guild, "Админ-выдача", f"Админ: {inter.author.mention}\nКому: {member.mention}\nСумма: {amount}", color=0xf1c40f)

# --- [ МАГАЗИН И ТИКЕТЫ ] ---

SHOP_ITEMS = {
    "role_oligarch": ("Роль 'Олигарх'", 15000, "👑 Высший социальный статус и уникальный цвет."),
    "role_rich": ("Роль 'Богатый'", 7500, "💰 Отличительная роль состоятельного пользователя."),
    "p_role": ("Персональная роль", 5000, "🎭 Создание личной роли с вашим названием."),
    "p_ext": ("Продление роли", 3000, "⏳ Продление действия персональной роли на месяц."),
    "p_grad": ("Градиент", 1500, "🌈 Красивый переливающийся ник в списке."),
    "p_edit": ("Изменение роли", 1000, "✏️ Редактирование названия или цвета вашей роли.")
}

class ShopSelect(disnake.ui.Select):
    def __init__(self):
        opts = [disnake.SelectOption(label=f"{v[0]} ({v[1]} {CURR_SYMBOL})", description=v[2], value=k) for k, v in SHOP_ITEMS.items()]
        super().__init__(placeholder="🛒 Выберите товар в магазине...", options=opts)

    async def callback(self, inter):
        data = await get_data(inter.author.id)
        name, price, desc = SHOP_ITEMS[self.values[0]]
        if data[0] < price: return await inter.send("❌ Недостаточно Aura Credits!", ephemeral=True)
        await update_db(inter.author.id, -price)
        pings = " ".join([f"<@&{rid}>" for rid in MOD_ROLES_IDS])
        
        cat = disnake.utils.get(inter.guild.categories, name=TICKET_CATEGORY_NAME)
        overwrites = {inter.guild.default_role: disnake.PermissionOverwrite(read_messages=False), inter.author: disnake.PermissionOverwrite(read_messages=True, send_messages=True)}
        for rid in MOD_ROLES_IDS:
            r = inter.guild.get_role(rid)
            if r: overwrites[r] = disnake.PermissionOverwrite(read_messages=True, send_messages=True)
        
        ch = await inter.guild.create_text_channel(f"заказ-{inter.author.name}", category=cat, overwrites=overwrites)
        emb = disnake.Embed(title="🛒 Новый заказ", description=f"Покупатель: {inter.author.mention}\nТовар: **{name}**\n\nОжидайте персонал.", color=0x2ecc71)
        btn = disnake.ui.Button(label="Закрыть", style=disnake.ButtonStyle.red, emoji="🔒")
        async def close(i):
            if any(i.author.get_role(rid) for rid in MOD_ROLES_IDS):
                await log_ticket_final(ch, i.author, inter.author, "ПОКУПКА")
                await ch.delete()
        btn.callback = close
        await ch.send(content=pings, embed=emb, view=disnake.ui.View(timeout=None).add_item(btn))
        await inter.send(f"✅ Заказ оформлен в {ch.mention}", ephemeral=True)

@bot.slash_command(name="shop", description="🛒 Посмотреть доступные товары в магазине")
async def shop(inter):
    await inter.send("🛍️ **Магазин Aura Sentinel**", view=disnake.ui.View().add_item(ShopSelect()))

@bot.slash_command(name="report_bug", description="🐞 Сообщить о баге или технической ошибке")
async def report_bug(inter):
    pings = " ".join([f"<@&{rid}>" for rid in MOD_ROLES_IDS])
    cat = disnake.utils.get(inter.guild.categories, name=TICKET_CATEGORY_NAME)
    overwrites = {inter.guild.default_role: disnake.PermissionOverwrite(read_messages=False), inter.author: disnake.PermissionOverwrite(read_messages=True, send_messages=True)}
    for rid in MOD_ROLES_IDS:
        r = inter.guild.get_role(rid)
        if r: overwrites[r] = disnake.PermissionOverwrite(read_messages=True, send_messages=True)
    
    ch = await inter.guild.create_text_channel(f"баг-{inter.author.name}", category=cat, overwrites=overwrites)
    emb = disnake.Embed(title="🐞 Баг-репорт", description=f"Отправитель: {inter.author.mention}\nОпишите проблему ниже.", color=0xe67e22)
    btn = disnake.ui.Button(label="Закрыть", style=disnake.ButtonStyle.red, emoji="🔒")
    async def close(i):
        if any(i.author.get_role(rid) for rid in MOD_ROLES_IDS):
            await log_ticket_final(ch, i.author, inter.author, "БАГ-РЕПОРТ")
            await ch.delete()
    btn.callback = close
    await ch.send(content=pings, embed=emb, view=disnake.ui.View(timeout=None).add_item(btn))
    await inter.send(f"✅ Баг-тикет открыт: {ch.mention}", ephemeral=True)

# --- [ ИГРЫ И ОСТАЛЬНОЕ ] ---

@bot.slash_command(name="blackjack", description="🃏 Играть в Блэкджек против дилера (Комиссия 5%)")
async def blackjack(inter, bet: int):
    data = await get_data(inter.author.id)
    if bet < 10 or data[0] < bet: return await inter.send("❌ Недостаточно средств или ставка слишком мала!", ephemeral=True)
    v = BlackjackView(inter, bet)
    await inter.send(embed=await v.make_emb(), view=v)

@bot.slash_command(name="roulette", description="🎰 Испытать удачу в рулетке (Комиссия 5%)")
async def roulette(inter, bet: int):
    data = await get_data(inter.author.id)
    if bet < 10 or data[0] < bet: return await inter.send("❌ Ошибка ставки!", ephemeral=True)
    
    class RView(disnake.ui.View):
        def __init__(self, inter, bet):
            super().__init__(timeout=30)
            self.inter, self.bet = inter, bet
        async def roll(self, i, color):
            if i.author.id != self.inter.author.id: return
            res = random.choices(["red", "black", "green"], weights=[49.5, 49.5, 1])[0]
            if color == res:
                win = int((self.bet * (35 if res == "green" else 2)) * 0.95)
                await update_db(self.inter.author.id, win - self.bet)
                m = f"🎉 Выпало **{res.upper()}**! Твой выигрыш: **{win}** {CURR_SYMBOL}"
            else:
                await update_db(self.inter.author.id, -self.bet)
                m = f"💀 Выпало **{res.upper()}**. Ты проиграл **{self.bet}** {CURR_SYMBOL}"
            await i.response.edit_message(content=m, view=None)
        @disnake.ui.button(label="Красное", style=disnake.ButtonStyle.danger)
        async def red(self, b, i): await self.roll(i, "red")
        @disnake.ui.button(label="Черное", style=disnake.ButtonStyle.secondary)
        async def black(self, b, i): await self.roll(i, "black")
        @disnake.ui.button(label="Зеленое", style=disnake.ButtonStyle.success)
        async def green(self, b, i): await self.roll(i, "green")

    await inter.send(f"🎰 Ставка: **{bet}** {CURR_SYMBOL}. Выбирай цвет:", view=RView(inter, bet))

@bot.slash_command(name="pay", description="💸 Передать Aura Credits другому игроку")
async def pay(inter, member: disnake.Member, amount: int):
    data = await get_data(inter.author.id)
    if member.id == inter.author.id or amount <= 0 or data[0] < amount: 
        return await inter.send("❌ Невозможная сумма или нехватка средств!", ephemeral=True)
    await update_db(inter.author.id, -amount); await update_db(member.id, amount)
    await inter.send(f"💸 {inter.author.mention} передал **{amount}** {CURR_SYMBOL} игроку {member.mention}")

@bot.slash_command(name="reward", description="🎁 Получить ежедневный бонус (300 AC)")
async def reward(inter):
    d = await get_data(inter.author.id)
    if int(time.time()) - d[2] < 86400: return await inter.send("⏳ Вы уже забирали бонус. Приходите завтра!", ephemeral=True)
    await update_db(inter.author.id, 300); await update_db(inter.author.id, int(time.time()), "reward")
    await inter.send(f"🎁 Вы получили **300** {CURR_SYMBOL}!")

@bot.slash_command(name="donate", description="❤️ Пожертвовать Aura Credits на развитие сервера")
async def donate_cmd(inter, amount: int):
    data = await get_data(inter.author.id)
    if amount <= 0 or data[0] < amount: return await inter.send("❌ У вас нет такой суммы!", ephemeral=True)
    await update_db(inter.author.id, -amount); await update_db(inter.author.id, amount, "donate")
    await inter.send(f"❤️ Огромное спасибо! Вы пожертвовали **{amount}** {CURR_SYMBOL}!")

# --- [ ЗАПУСК БОТА ] ---
@bot.event
async def on_ready():
    await init_db()
    await bot.change_presence(activity=disnake.Game(name="Aura Sentinel"))
    await bot._sync_application_commands()
    print(f"🚀 Aura Sentinel онлайн (Neon DB Connected)!")

bot.run(TOKEN)