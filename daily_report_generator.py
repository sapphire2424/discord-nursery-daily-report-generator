import os
import datetime
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from openai import OpenAI

# 1. 環境設定と定数
load_dotenv()
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
GUILD_ID = int(os.getenv('GUILD_ID'))
SUMMARY_CHANNEL_ID = int(os.getenv('SUMMARY_CHANNEL_ID'))
EXCLUDED_CHANNEL_IDS = []  # AIに読み込ませないチャンネルIDがある場合はここに追加

# 共通タイムゾーン設定 (JST)
JST = datetime.timezone(datetime.timedelta(hours=9))

# クライアント初期化
client_ai = OpenAI(api_key=OPENAI_API_KEY)
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- 2. AIロジック ---

async def summarize_nursery_log(server_data, target_date_str):
    """
    提供されたログから事実のみを抽出して要約する
    """
    prompt = f"""
あなたはこども園の主任保育士です。
提供された2日間分のログを読み、**【{target_date_str}】（当日）の分だけ**を業務日報としてまとめてください。

【重要：抽出と記載のルール】
1. **重要事項の見逃し厳禁**: 「明日」「確認」「依頼」「検討」「お願い」「TODO」など、次に行うべきアクションが含まれる発言はできる限り拾ってください。
2. **「明日以降への申し送り」の重視**: 当日のログの中で、翌日以降の予定やタスクに関する内容はすべてこのセクションに集約してください。
3. **事実のみを記載**: ログにある言葉だけを使用してください。AIが「〜を予定しています」「〜の準備をしましょう」のように勝手に文章を膨らませたり、推測で肉付けしたりすることは一切禁止です。アドバイス、架空の活動の付け足しは一切禁止です。
4. **情報の鮮度**: 前日のログは背景としてのみ使い、日報の中身は必ず当日の事実で構成してください。
5. **記載がない場合は飛ばす**: ログに情報がないクラスや項目は、空欄を埋めるために創作せず、項目ごと削除するか「特記事項なし」としてください。
6. **呼称について**: 職員には〇〇先生、預かっている人には〇〇としてください。
7. **重複の禁止**: 同じ内容を複数のセクションに書かないでください。
8. **クラス情報の徹底移動**: ログ内に特定のクラス名（ぞう、きりん等）が含まれる出来事は、必ず「■ クラス別の報告」に記載し、「園全体のトピック」には含めないでください。
9. **discord idが名前でない場合**:そのままdiscord idのまま〇〇先生と書いてください。 
10. **職員の用意**:明日以降への申し送りで、指定の持ち物および指定する服装での出勤が必要な場合必ず記載すること(対象者も記載すること。)

【構成】
■ **職員の勤怠**
（{target_date_str}の欠席・遅刻等の事実のみ。〇〇先生の形式で記載）

■ **園全体のトピック**
（{target_date_str}に行われた決定・報告・行事の事実のみ）

■ **クラス別の報告**
（クラス名：出席/欠席状況。{target_date_str}のログにある具体的な活動や園児の様子。ログに記載がない活動は絶対に書かないこと）

■ **フォーラム・掲示板の動き**
（{target_date_str}に書き込まれた議論・決定事項のみ）

■ **明日以降への申し送り**
（今日解決しなかったタスクや、明示されている連絡事項のみ）

--- ログデータ ---
{server_data}
"""
    response = client_ai.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "あなたは事実のみを正確に要約する専門家です。推測や創作を一切排除します。"},
            {"role": "user", "content": prompt}
        ],
        temperature=0.0
    )
    return response.choices[0].message.content

# --- 3. ユーティリティ ---

async def send_long_message(channel, text):
    """Discordの2000文字制限を考慮して分割送信"""
    for i in range(0, len(text), 2000):
        await channel.send(text[i:i+2000])

async def get_messages_from_history(channel_or_thread, start, end):
    """指定期間のログを取得"""
    logs = ""
    async for message in channel_or_thread.history(after=start, before=end, limit=None, oldest_first=True):
        if not message.author.bot and message.content.strip():
            time_str = message.created_at.astimezone(JST).strftime('%m/%d %H:%M')
            logs += f"[{time_str}] {message.author.display_name}: {message.content}\n"
    return logs

async def generate_and_post_report():
    """日報生成のコアロジック"""
    guild = bot.get_guild(GUILD_ID)
    summary_channel = bot.get_channel(SUMMARY_CHANNEL_ID)
    
    if not guild or not summary_channel:
        print("サーバーまたは送信先チャンネルが見つかりません。")
        return

    now = datetime.datetime.now(JST)
    # 当日の00:00から実行時点までのログを取得
    target_date_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    prev_date_start = target_date_start - datetime.timedelta(days=1) # 前日分も含めてコンテキストとする場合
    
    target_date_str = target_date_start.strftime('%Y/%m/%d')
    print(f"{target_date_str} のログ集計を開始します...")

    all_logs = ""
    for channel in guild.channels:
        if channel.id in EXCLUDED_CHANNEL_IDS or channel.id == SUMMARY_CHANNEL_ID:
            continue

        # テキストチャンネル
        if isinstance(channel, discord.TextChannel):
            log = await get_messages_from_history(channel, prev_date_start, now)
            if log:
                all_logs += f"\n### チャンネル: {channel.name}\n{log}"

        # フォーラムチャンネル
        elif isinstance(channel, discord.ForumChannel):
            forum_logs = ""
            # 公開スレッド
            for thread in channel.threads:
                thread_log = await get_messages_from_history(thread, prev_date_start, now)
                if thread_log: forum_logs += f"\n[投稿: {thread.name}]\n{thread_log}"
            # アーカイブ済みスレッド（直近10件）
            async for thread in channel.archived_threads(limit=10):
                thread_log = await get_messages_from_history(thread, prev_date_start, now)
                if thread_log: forum_logs += f"\n[投稿(アーカイブ): {thread.name}]\n{thread_log}"
            
            if forum_logs:
                all_logs += f"\n### フォーラム: {channel.name}\n{forum_logs}"

    if all_logs:
        summary = await summarize_nursery_log(all_logs, target_date_str)
        res_msg = f"━━━━━━━━━━━━━━━━━━━━━━━━\n📅 **{target_date_str} 業務日報**\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n" + summary
        await send_long_message(summary_channel, res_msg)
        print(f"{target_date_str} の日報を送信しました。")
    else:
        print("対象となるメッセージが見つかりませんでした。")

# --- 4. ボットのイベント・コマンド ---

@tasks.loop(time=datetime.time(hour=18, minute=30, tzinfo=JST))
async def daily_report_task():
    """毎日18:30に自動実行"""
    print("定刻になりました。日報生成を開始します...")
    await generate_and_post_report()

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')
    if not daily_report_task.is_running():
        daily_report_task.start()

bot.run(DISCORD_TOKEN)