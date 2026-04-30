import os
import requests
import telebot
import random
import json
from loguru import logger
from dotenv import load_dotenv
from haystack import component, Document
from haystack.utils import Secret
from haystack.components.generators.chat import OpenAIChatGenerator
from haystack.components.embedders import OpenAITextEmbedder
from haystack.dataclasses import ChatMessage
from haystack.tools import ComponentTool
from haystack.components.websearch import SerperDevWebSearch
from pinecone import Pinecone, ServerlessSpec
from haystack_integrations.document_stores.pinecone import PineconeDocumentStore
from openai import OpenAI
from haystack.components.agents import Agent

# Load environment variables
load_dotenv()

# Configuration
PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
PINECONE_INDEX_NAME = os.getenv("PINECONE_INDEX_NAME", "haystack-agent")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# User's .env uses PROXY_API_KEY for OpenAI and OPENAI_BASE_URL for proxy
OPENAI_API_KEY = os.getenv("PROXY_API_KEY") 
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY", "demo")
FINAGE_API_KEY = os.getenv("FINAGE_API_KEY")
SERPERDEV_API_KEY = os.getenv("SERPERDEV_API_KEY")

# Настройка логирования
logger.add("logs/app.log", rotation="500 MB", level="INFO")
logger.add("logs/tools.log", filter=lambda record: "tool" in record["extra"], level="DEBUG")

# OpenAI модель для эмбеддингов
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
# OpenAI модель для генерации ответов
CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-4o-mini")

# Initialize Pinecone
pc = Pinecone(api_key=PINECONE_API_KEY)
if PINECONE_INDEX_NAME not in pc.list_indexes().names():
    pc.create_index(
        name=PINECONE_INDEX_NAME,
        dimension=1536, # Dimension for text-embedding-3-small
        metric="cosine",
        spec=ServerlessSpec(cloud="aws", region="us-east-1")
    )

document_store = PineconeDocumentStore(
    index=PINECONE_INDEX_NAME,
    namespace="chat-history",
    dimension=1536
)

# Initialize Embedder for retrieval
embedder = OpenAITextEmbedder(api_key=Secret.from_token(OPENAI_API_KEY), api_base_url=OPENAI_BASE_URL, model=EMBEDDING_MODEL)

# Initialize Telegram Bot
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# --- Tools Implementation ---

@component
class AlphaVantageFinancialFact:
    """
    Fetches a random financial news/fact from Alpha Vantage.
    """
    @component.output_types(fact=str)
    def run(self):
        t_logger = logger.bind(tool="financial_fact")
        t_logger.info("Fetching financial fact from Alpha Vantage")
        # Using News & Sentiment API as a source for financial facts
        url = f"https://www.alphavantage.co/query?function=NEWS_SENTIMENT&apikey={ALPHAVANTAGE_API_KEY}"
        try:
            response = requests.get(url)
            data = response.json()
            if "feed" in data and len(data["feed"]) > 0:
                item = data["feed"][0]
                fact = f"Интересный факт из мира финансов: {item['title']}. {item['summary'][:200]}..."
                t_logger.debug(f"Successfully fetched fact: {item['title']}")
                return {"fact": fact}
            t_logger.warning("No news feed items found in Alpha Vantage response")
            return {"fact": "В данный момент свежих финансовых фактов нет."}
        except Exception as e:
            t_logger.error(f"Error fetching financial fact: {str(e)}")
            return {"fact": f"Ошибка при получении факта: {str(e)}"}

@component
class AlphaVantageRandomImage:
    """
    Gets a real financial chart image and describes it using OpenAI Vision.
    Returns a structured JSON string with image_url and description.
    """
    @component.output_types(result=str)
    def run(self, symbol: str = "TSLA"):
        # If no symbol is provided, try to extract from common financial requests
        if symbol == "AAPL":
            # Try to detect common stock symbols from typical financial requests
            # This is a simple heuristic - in production, use proper NLP/entity extraction
            # For now, use a pattern-based approach to match common symbols
            import re
            # Look for common stock symbols in typical financial contexts
            common_patterns = [
                r'\bTSLA\b', r'\bMSFT\b', r'\bGOOGL\b', r'\bAMZN\b',
                r'\bBTC\b', r'\bETH\b', r'\bAAPL\b', r'\bNVDA\b'
            ]
            # In a real implementation, we would have access to the user's message
            # For now, use a fallback to TSLA as it's commonly requested
            symbol = "TSLA"
        t_logger = logger.bind(tool="image_analysis")
        t_logger.info("Starting financial image analysis process")
        
        # Using Finviz chart service (requires User-Agent header for proper access)
        # Validate symbol format - remove any non-alphanumeric characters
        symbol = ''.join(c for c in symbol if c.isalnum())
        if not symbol:
            symbol = "AAPL"
        image_url = f"https://finviz.com/chart.ashx?t={symbol}&ty=c&ta=1&p=d&s=l"
        
        t_logger.info(f"Selected symbol {symbol}, fetching chart from {image_url}")
        
        client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
        
        try:
            t_logger.debug("Sending request to OpenAI Vision model")
            response = client.chat.completions.create(
                model=CHAT_MODEL,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Ты - эксперт по финансовому анализу. Опиши этот график инструмента {symbol}. Что на нем изображено, текущий тренд, краткая предыстория этой информации и твоя рекомендация (buy/sell/hold) с обоснованием. Ответ сформируй в один связный текст."},
                            {
                                "type": "image_url",
                                "image_url": {"url": image_url},
                            },
                        ],
                    }
                ],
                max_tokens=800,
            )
            description = response.choices[0].message.content
            t_logger.info("Successfully received description from OpenAI Vision")
            
            result = {
                "image_url": image_url,
                "description": description,
                "type": "financial_chart_analysis"
            }
            return {"result": json.dumps(result)}
        except Exception as e:
            t_logger.error(f"Error during image analysis: {str(e)}")
            error_result = {
                "error": str(e),
                "type": "error"
            }
            return {"result": json.dumps(error_result)}

# --- Agent Setup ---

fact_tool = ComponentTool(component=AlphaVantageFinancialFact())
image_tool = ComponentTool(component=AlphaVantageRandomImage())
search_tool = ComponentTool(component=SerperDevWebSearch(api_key=Secret.from_token(SERPERDEV_API_KEY)))

agent = Agent(
    chat_generator=OpenAIChatGenerator(
        model=CHAT_MODEL,
        api_key=Secret.from_token(OPENAI_API_KEY),
        api_base_url=OPENAI_BASE_URL
    ),
    system_prompt="""Ты - умный персональный помощник. Твоя задача - помогать пользователю, учитывая контекст прошлых сообщений.
    Ты можешь предоставлять финансовые факты, анализировать изображения финансовых графиков и выполнять веб-поиск через Google.
    При анализе финансовых графиков, извлекай символ инструмента (например, TSLA, AAPL, MSFT) из запроса пользователя и используй его для получения соответствующего графика.
    Всегда отвечай вежливо и профессионально на русском языке.""",
    tools=[fact_tool, image_tool, search_tool]
)

# --- Memory Management ---

def get_user_history(user_id, limit=5):
    """Retrieves recent chat history for the user from Pinecone."""
    try:
        # Embedding the user_id to search for relevant context
        # In a real app, we might search by semantic content, 
        # but here we'll filter by user_id in metadata if supported, 
        # or just fetch recent docs for simplicity.
        
        # For PineconeDocumentStore, we can use filter
        filters = {"field": "user_id", "operator": "==", "value": user_id}
        docs = document_store.filter_documents(filters=filters)
        
        # Sort by timestamp (if we had one) or just take last ones
        # Since we don't have a reliable way to sort in this simple store without custom metadata,
        # we'll just take the available docs.
        history = []
        for doc in docs[-limit:]:
            history.append(ChatMessage.from_user(doc.meta.get("user_input", "")))
            history.append(ChatMessage.from_assistant(doc.meta.get("assistant_output", "")))
        return history
    except Exception as e:
        print(f"Error retrieving history: {e}")
        return []

def save_interaction(user_id, user_input, assistant_output):
    """Saves the interaction to Pinecone."""
    try:
        # We need an embedding for the document to store it in PineconeDocumentStore
        # Store only the user's message (not the assistant's response) as requested
        text_to_embed = user_input
        embedding_result = embedder.run(text=text_to_embed)
        embedding = embedding_result["embedding"]
        
        from dataclasses import replace
        
        doc = Document(
            content=user_input,
            embedding=embedding,
            meta={
                "user_id": user_id,
                "user_input": user_input
            }
        )
        document_store.write_documents([doc])
    except Exception as e:
        print(f"Error saving interaction: {e}")

# --- Telegram Bot Handlers ---

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "Привет! Я твой персональный помощник на базе Haystack и Pinecone. Чем могу помочь?")

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    user_id = str(message.from_user.id)
    user_text = message.text
    logger.info(f"Received message from user {user_id}: {user_text}")
    
    # 1. Получаем историю диалога
    history = get_user_history(user_id)
    
    # 2. Добавляем текущее сообщение
    messages = history + [ChatMessage.from_user(user_text)]
    
    try:
        # 3. Запускаем агента
        result = agent.run(messages=messages)
        response_content = result['last_message'].text
        
        # Проверяем, является ли ответ JSON-объектом от инструмента анализа изображений
        try:
            data = json.loads(response_content)
            if isinstance(data, dict) and data.get("type") == "financial_chart_analysis":
                image_url = data.get("image_url")
                description = data.get("description")
                logger.info(f"Agent returned chart analysis for user {user_id}. Sending photo.")
                bot.send_photo(message.chat.id, image_url, caption=description[:1024]) # Telegram caption limit
                save_interaction(user_id, user_text, description)
                return
            elif isinstance(data, dict) and data.get("type") == "error":
                error_msg = data.get("error", "Unknown error")
                logger.error(f"Agent tool returned error: {error_msg}")
                bot.reply_to(message, f"Ошибка при работе инструмента: {error_msg}")
                return
        except json.JSONDecodeError:
            # Не JSON, обычный текстовый ответ
            pass

        # 4. Сохраняем контекст в Pinecone
        save_interaction(user_id, user_text, response_content)
        
        logger.info(f"Sending text response to user {user_id}")
        bot.reply_to(message, response_content)
        
    except Exception as e:
        logger.exception(f"Error handling message from user {user_id}")
        bot.reply_to(message, f"Произошла ошибка: {str(e)}")

if __name__ == "__main__":
    print("Бот запущен...")
    bot.polling()
