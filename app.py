import os
import time

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from dotenv import load_dotenv
from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings
from langchain_community.vectorstores.azuresearch import AzureSearch
from langchain.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough
from langchain_core.output_parsers import StrOutputParser
from openai import RateLimitError
import logging
import json

# --- Initialize Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Load Environment Variables ---
load_dotenv()

# --- Global Variable to Hold the Cached RAG Chain ---
rag_chain_instance_global = None

# --- Get environment variables (ensure these are correctly loaded) ---
AZURE_OPENAI_ENDPOINT_EMBEDDINGS = os.getenv("AZURE_OPENAI_ENDPOINT_EMBEDDINGS")
AZURE_OPENAI_API_KEY_EMBEDDINGS = os.getenv("AZURE_OPENAI_API_KEY_EMBEDDINGS")
AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME")
AZURE_OPENAI_API_VERSION_EMBEDDINGS = os.getenv("AZURE_OPENAI_API_VERSION_EMBEDDINGS")

AZURE_OPENAI_ENDPOINT_CHAT = os.getenv("AZURE_OPENAI_ENDPOINT_CHAT")
AZURE_OPENAI_API_KEY_CHAT = os.getenv("AZURE_OPENAI_API_KEY_CHAT")
AZURE_OPENAI_CHAT_DEPLOYMENT_NAME = os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT_NAME")
AZURE_OPENAI_API_VERSION_CHAT = os.getenv("AZURE_OPENAI_API_VERSION_CHAT")

AZURE_AI_SEARCH_ENDPOINT = os.getenv("AZURE_AI_SEARCH_ENDPOINT")
AZURE_AI_SEARCH_API_KEY = os.getenv("AZURE_AI_SEARCH_API_KEY")
AZURE_AI_SEARCH_INDEX_NAME = os.getenv("AZURE_AI_SEARCH_INDEX_NAME")


def create_rag_chain():
    logger.info("Attempting to initialize RAG chain...")
    # Environment variable checks (as before)
    if not all(
            [AZURE_OPENAI_ENDPOINT_EMBEDDINGS, AZURE_OPENAI_API_KEY_EMBEDDINGS, AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME,
             AZURE_OPENAI_API_VERSION_EMBEDDINGS]):
        logger.error("Azure OpenAI Embeddings environment variables are not fully set.")
        return None
    if not all([AZURE_OPENAI_ENDPOINT_CHAT, AZURE_OPENAI_API_KEY_CHAT, AZURE_OPENAI_CHAT_DEPLOYMENT_NAME,
                AZURE_OPENAI_API_VERSION_CHAT]):
        logger.error("Azure OpenAI Chat environment variables are not fully set.")
        return None
    if not all([AZURE_AI_SEARCH_ENDPOINT, AZURE_AI_SEARCH_API_KEY, AZURE_AI_SEARCH_INDEX_NAME]):
        logger.error("Azure AI Search environment variables are not fully set.")
        return None

    try:
        embeddings = AzureOpenAIEmbeddings(
            azure_deployment=AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME,
            azure_endpoint=AZURE_OPENAI_ENDPOINT_EMBEDDINGS,
            api_key=AZURE_OPENAI_API_KEY_EMBEDDINGS,
            api_version=AZURE_OPENAI_API_VERSION_EMBEDDINGS,
        )
        vector_store = AzureSearch(
            azure_search_endpoint=AZURE_AI_SEARCH_ENDPOINT,
            azure_search_key=AZURE_AI_SEARCH_API_KEY,
            index_name=AZURE_AI_SEARCH_INDEX_NAME,
            embedding_function=embeddings.embed_query
        )
        # Initialize LLM: streaming=True is not explicitly needed here for AzureChatOpenAI
        llm = AzureChatOpenAI(
            azure_deployment=AZURE_OPENAI_CHAT_DEPLOYMENT_NAME,
            azure_endpoint=AZURE_OPENAI_ENDPOINT_CHAT,
            api_key=AZURE_OPENAI_API_KEY_CHAT,
            api_version=AZURE_OPENAI_API_VERSION_CHAT,
            temperature=0.3,
            max_tokens=1500
        )
        retriever = vector_store.as_retriever(search_type="hybrid")

        template = """You are an expert AI assistant for our application. Your goal is to be insightful and proactive.
        Answer the question based on the following context.
        If the context is empty or doesn't contain the answer, clearly state that you don't have enough information from the provided documents to answer.
        Do not use any external information. Be concise in your primary answer.

        Context:
        {context}

        Question: {question}

        ---
        Primary Answer:
        [Provide a direct answer to the question based on the context. Cite sources if possible, e.g., (Source: application_docs.md)]

        ---
        Predictive Insights & Next Steps:
        Based on the question and the provided context:
        1. Are there any related topics or proactive suggestions you can offer?
        2. If the context discusses an issue or a process, what are the key implications or next logical steps?
        (If no specific predictive insights are apparent from this context, state "No specific predictive insights or next steps apparent from this context.")
        """
        prompt = ChatPromptTemplate.from_template(template)

        def format_docs(docs):
            if not docs:
                return "No relevant context found in the documents."
            return "\n\n".join(
                f"Source: {doc.metadata.get('source', 'Unknown')}\nContent: {doc.page_content}" for doc in docs)

        rag_chain = (
                {"context": retriever | format_docs, "question": RunnablePassthrough()}
                | prompt
                | llm
                | StrOutputParser()  # This ensures chunks are strings
        )
        logger.info("RAG chain initialized successfully.")
        return rag_chain
    except Exception as e:
        logger.error(f"Failed to initialize RAG chain: {e}", exc_info=True)
        return None


# --- Flask App Definition ---
app = Flask(__name__)
CORS(app)

# --- Initialize RAG chain on application startup ---
with app.app_context():
    rag_chain_instance_global = create_rag_chain()
    if rag_chain_instance_global is None:
        logger.critical("CRITICAL: RAG Chain could not be initialized on startup. The /chat endpoint will not work.")


@app.route('/chat', methods=['POST'])
def chat_endpoint():
    global rag_chain_instance_global

    if rag_chain_instance_global is None:
        logger.error("RAG chain is not initialized. Cannot process request.")
        return jsonify({"error": "RAG chain not initialized. Server configuration issue."}), 500

    try:
        data = request.get_json()
        if not data or 'query' not in data:
            logger.warning("Received bad request: 'query' field missing.")
            return jsonify({"error": "Missing 'query' field in request body"}), 400

        user_query = data['query']
        logger.info(f"Received query for streaming: {user_query}")

        def generate_stream():
            try:
                for chunk in rag_chain_instance_global.stream(user_query):
                    if chunk:  # Ensure chunk is not empty
                        sse_formatted_chunk = f"data: {json.dumps({'token': chunk})}\n\n"
                        yield sse_formatted_chunk
                        time.sleep(0.05)
                # Optionally, send a special "end-of-stream" event if your client needs it
                # yield f"event: end-stream\ndata: {{}}\n\n"
            except RateLimitError as rle_stream:
                logger.warning(f"Rate limit exceeded during stream: {rle_stream}")
                error_payload = json.dumps(
                    {"error": "API rate limit exceeded during stream. Please try again later.", "code": 429})
                yield f"event: error\ndata: {error_payload}\n\n"
            except Exception as e_stream:
                logger.error(f"Error during stream generation: {e_stream}", exc_info=True)
                error_payload = json.dumps({"error": f"An error occurred during streaming: {str(e_stream)}"})
                yield f"event: error\ndata: {error_payload}\n\n"
            finally:
                logger.info(f"Stream ended for query: {user_query}")

        # Return a Flask Response object with the generator and correct mimetype for SSE
        return Response(generate_stream(), mimetype='text/event-stream')

    except RateLimitError as rle:  # Handles errors before streaming starts
        logger.warning(f"Rate limit exceeded (before stream): {rle}")
        return jsonify({"error": "API rate limit exceeded. Please try again later."}), 429
    except Exception as e:  # Handles other errors before streaming starts
        logger.error(f"Error processing chat request (before stream): {e}", exc_info=True)
        return jsonify({"error": f"An internal error occurred: {str(e)}"}), 500


# --- /health endpoint (as before) ---
@app.route('/health', methods=['GET'])
def health_check():
    global rag_chain_instance_global
    if rag_chain_instance_global is not None:
        return jsonify({
            "status": "ok",
            "message": "Flask application is running and RAG chain is initialized.",
            "rag_chain_initialized": True
        }), 200
    else:
        logger.error("Health check failed: RAG chain is not initialized.")
        return jsonify({
            "status": "error",
            "message": "Flask application is running, but the RAG chain failed to initialize. Check server logs for details.",
            "rag_chain_initialized": False
        }), 503


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001)