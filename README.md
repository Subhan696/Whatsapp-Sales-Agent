# WhatsApp Business AI Sales Agent & CRM

A full-stack, autonomous WhatsApp AI Sales Agent and integrated Customer Relationship Management (CRM) dashboard. 

This system uses an advanced LLM architecture to converse with customers on WhatsApp, dynamically browse product catalogs, quote accurate pricing (including delivery policies), manage shopping carts, handle order corrections, and guide customers through both Cash on Delivery (COD) and Bank Transfer workflows. 

## 🚀 Key Features

*   **Autonomous Conversational Sales**:
    *   Understands complex user intents (catalog queries, price negotiations, order modifications).
    *   Strict anti-hallucination protocols for SKUs and pricing.
    *   Dynamic payment method switching (COD ↔ Bank Transfer) without losing order context.
    *   Automated invoice formatting and transmission directly in the chat.
    *   Graceful cancellation handling and retention attempts.
*   **Integrated CRM Dashboard**:
    *   Mobile-responsive web interface for admins.
    *   Real-time chat logs and conversation history.
    *   Order management (approve, cancel, track statuses like "Awaiting Payment").
    *   Customer tracking across different CRM stages (lead, interested, closed won, etc.).
*   **Intelligent State Management**:
    *   Powered by LangGraph for cyclic agent workflows.
    *   PostgreSQL checkpointer for cross-turn conversational memory.
*   **Custom WhatsApp Bridge**:
    *   Dedicated Node.js bridge service to handle real-time WhatsApp Web protocols.

## 🛠 Technology Stack

### Backend
*   **Python 3.11+**
*   **FastAPI** & **Uvicorn** (High-performance async API)
*   **LangGraph** & **LangChain** (Agent state orchestration and LLM tooling)
*   **Anthropic / OpenAI** (Large Language Models)
*   **SQLAlchemy (Async)** & **asyncpg** (ORM and Database Drivers)
*   **Alembic** (Database migrations)
*   **Pydantic** (Data validation and settings management)

### Infrastructure & Deployment
*   **PostgreSQL 16** (Primary transactional database and agent state store)
*   **Docker & Docker Compose** (Containerised multi-service deployment)
*   **Nginx** (Reverse proxy and static file server)
*   **Certbot** (Automated SSL certificates)

### WhatsApp Integration
*   **wa-bridge** (Node.js microservice bridging WhatsApp protocols to the Python backend via Webhooks)

## 🏗 System Architecture

The project consists of multiple Dockerised services defined in `docker-compose.yml`:
1.  **`app`**: The core Python FastAPI backend running the LangGraph agent and serving the CRM admin dashboard.
2.  **`bridge`**: The Node.js WhatsApp connection layer.
3.  **`postgres`**: The database for products, orders, customers, chat history, and LangGraph checkpoints.
4.  **`nginx`**: Handles incoming web traffic, SSL termination, and serves uploaded product media.
5.  **`certbot`**: Manages automatic Let's Encrypt SSL certificate renewals.

## ⚡ Agent Skills & Tools

The AI agent is equipped with several backend tools to execute real-world actions:
*   `search_catalog`: Queries the local Postgres database (or Shopify API) to find products based on user requests (e.g., "cheapest item", "smartwatches").
*   `send_product_media`: Triggers the bridge to send product images/videos directly in the WhatsApp chat.
*   `create_order`: Generates a formal invoice, calculates subtotal + delivery charges, and registers the order in the CRM.
*   `update_payment_method`: Allows seamless switching between COD and Bank Transfer for active orders.
*   `cancel_order`: Voids incorrect or unwanted orders.
*   `update_crm` / `flag_cancellation_pending` / `request_refund`: Internal tools to manage the customer's lifecycle stage in the admin dashboard.

## 🚀 Getting Started

### Prerequisites
*   Docker & Docker Compose
*   Python 3.11+ (if running locally without Docker)
*   OpenAI or Anthropic API Keys

### Running with Docker

1.  Clone the repository.
2.  Copy `.env.example` to `.env` and fill in your database credentials, API keys, and configuration.
3.  Run the application:
    ```bash
    docker compose up -d
    ```
4.  The CRM dashboard will be available at `http://localhost:8000` (or your configured domain).

### Local Development

1.  Install dependencies using `hatch` or standard pip:
    ```bash
    pip install -e .[dev]
    ```
2.  Run Alembic migrations to initialize the local SQLite or Postgres database:
    ```bash
    alembic upgrade head
    ```
3.  Start the FastAPI server:
    ```bash
    uvicorn app.main:app --reload
    ```
