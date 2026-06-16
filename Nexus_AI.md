# 🚀 Nexus AI: Collaborative Context-Aware Developer Assistant

## 📌 Project Overview
**Nexus AI**  is an enterprise-grade, "over-the-shoulder" AI assistant designed specifically for software development teams. It acts as a continuous presence that silently tracks a developer's active screen context using lightweight text-logging, assists with code via local LLMs, and facilitates seamless, context-rich communication between team members. 

By leveraging local open-source AI models and Vector Databases, the system achieves human-like memory of past coding sessions without relying on expensive cloud APIs or heavy, continuous image processing.

---

## ✨ Core Functionalities & Capabilities

### 1. Lightweight Screen Tracking (Zero Image Uploads)
* **OS Window Tracking:** Silently logs active application titles and file names (e.g., `views.py - VS Code`).
* **IDE Context Watching:** Monitors active code blocks and terminal errors without capturing heavy screenshots.
* **On-Demand OCR:** Only when the user explicitly asks a visual question (e.g., "Why is this UI misaligned?"), the system captures a single frame, extracts text via OCR, and processes the query locally.

### 2. The "Expert Developer" Local AI
* **Code Assistance:** Highlight code, hit a hotkey, and ask the local AI to debug, explain, or refactor.
* **Contextual Awareness:** The AI knows what file you are working on without you needing to paste the code manually.

### 3. Team Sync Hub (Contextual Pinging)
* **Cross-Team Communication:** User A can ask their AI a question for User B. The central server pushes an interactive popup (text/buttons) to User B's screen.
* **Shared Context:** Pings can include the specific code context User A is struggling with.

### 4. Human-like Memory (RAG + Vector Vault)
* **Semantic Logging:** Past activities and solved errors are converted into mathematical vectors and stored locally.
* **Recall:** Users can ask, "How did I fix that JWT error last Tuesday?" The AI searches the Vector DB, retrieves the relevant context, and answers dynamically.

### 5. Automated Daily Work Reports
* **Log Aggregation:** Text logs of active windows are compiled throughout the day.
* **AI Summarization:** At a scheduled time, the AI drafts a clean Markdown report of the day's tasks.
* **Verification:** The user reviews, edits, and approves the report via the UI before it is sent to the central manager dashboard.

---

## 🏗️ System Architecture & Data Flow

File generated: `/mnt/data/SyncJarvis_Project_Documentation.md`

```mermaid
graph TD
    %% Subgraph: Local Developer PC
    subgraph LocalPC ["Local Developer PC"]
        UI["React UI Desktop App"] 
        Tracker["Python Tracker (pygetwindow / psutil)"]
        VectorDB[("ChromaDB (Local Memory)")]
        Ollama{"Ollama (Local AI Models)"}
        
        Tracker -->|"Logs semantic text"| VectorDB
        UI <-->|"Prompts & Context"| Ollama
        Tracker -->|"Active File Name"| UI
    end

    %% Subgraph: Central Server / Cloud
    subgraph Cloud ["Central Server / Cloud"]
        Django["Django REST & Channels"]
        Celery["Celery + Redis (Background Tasks)"]
        DB[("PostgreSQL: Users & Reports)")]
        
        Django <--> Celery
        Django <--> DB
    end
    
    %% Subgraph: Team Member PC
    subgraph TeamPC ["Team Member PC"]
        MemberUI["React UI Desktop App"]
    end
    
    %% Inter-System Connections
    UI <-->|"WebSockets: Team Pings"| Django
    UI -->|"Approved Reports"| Django
    Django <-->|"WebSockets: Receives Ping"| MemberUI
```
---

## 💻 Technology Stack

| Component | Technology | Purpose |
| --- | --- | --- |
| **Frontend UI** | React.js (via Tauri or Electron) | Lightweight desktop interface with Enterprise Dark Mode. |
| **Local Tracker** | Python (`pygetwindow`, `psutil`) | Extremely low-resource active window and IDE tracking. |
| **Backend Server** | Django REST Framework (DRF) | Central API, user authentication (JWT), and RBAC. |
| **Real-Time Sync** | Django Channels (WebSockets) | Instant bidirectional communication for team pings. |
| **Async Processing** | Celery + Redis | Handles heavy report generation queues centrally. |
| **Memory Database** | ChromaDB or LanceDB | Local vector database for Retrieval-Augmented Generation (RAG). |

---

## 🤖 AI Model Strategy (Optimized for Local Execution)

Because the system relies on local hardware, models are carefully selected to balance high coding intelligence with low RAM/CPU footprint. **All models are run locally via Ollama.**

| Model Name | Parameters | Purpose | Hardware Impact |
| --- | --- | --- | --- |
| **`qwen2.5-coder:1.5b`** or **`3b`** | 1.5B - 3B | Core Logic, Code Debugging, General Chat | Low-Medium (Uses ~1.5 - 3GB RAM). Fast on CPUs. |
| **`llama3.2:1.5b`** or **`gemma2:2b`** | 1.5B - 2B | Summarizing daily text logs into Work Reports | Low (Runs only during report generation). |
| **`nomic-embed-text`** | < 0.5B | Converts text logs into semantic vectors for memory | Very Low (< 500MB). Runs instantly. |
| **`pytesseract`** (OCR Tool) | N/A | Extracts text from single screenshots when requested | Very Low. |

---

## ⚙️ Hardware & Software Requirements

Tailored explicitly for the **HP EliteDesk 800 G2 SFF** execution environment.

| Requirement | Specification / Status | Notes |
| --- | --- | --- |
| **OS** | Linux Ubuntu 22.04.5 LTS (64-bit) | Ideal lightweight OS for running backend services natively. |
| **Processor** | Intel® Core™ i5-6500 @ 3.20GHz (4 Cores) | Sufficient for tracking, WebSockets, and running `< 3B` parameter AI models. |
| **Memory (RAM)** | 16.0 GiB | **Crucial.** Provides enough headroom to run Ollama (~3GB), IDEs, and local databases simultaneously. |
| **Graphics** | Intel® HD Graphics 530 (Integrated) | No dedicated GPU. AI processing will rely entirely on CPU and system RAM. |
| **Storage** | 240.1 GB Disk Capacity | Sufficient. Small LLMs require ~2-4 GB each. ChromaDB uses minimal space. |

---

## 🛠️ Phased Development Roadmap

### Phase 1: The Local Observer (UI & Context)

* Build the React frontend (Dark mode aesthetic).
* Write the Python script to log active windows every 5 seconds.
* Ensure CPU usage on the i5-6500 remains $< 1\%$.

### Phase 2: The "Expert" Brain & Memory

* Install Ollama and pull `qwen2.5-coder:1.5b`.
* Integrate ChromaDB. Convert text logs to vectors using `nomic-embed-text`.
* Build the interface to query past logs ("What did I work on yesterday?").

### Phase 3: Team Sync Hub (Network)

* Setup Django, PostgreSQL, and simple JWT authentication.
* Configure Django Channels (WebSockets).
* Test sending a "Ping" payload from User A to User B's local React UI.

### Phase 4: Automation (Daily Reports)

* Configure Celery and Redis on the Ubuntu environment.
* Write tasks to pull end-of-day tracking logs.
* Use `llama3.2:1.5b` to format logs into Markdown reports for user approval.

---

# 🚀 Nexus AI: Project Overview & Roadmap (NonTech term)

This document explains the step-by-step plan to build **SyncJarvis**, a smart, secure, and private AI assistant for teams. Instead of using complex technical jargon, this roadmap focuses on the **goals, features, and value** of each phase so anyone can easily understand how the project comes together.

---

## 🛠️ Phase 1: The Foundation (The Assistant Learns to Watch)
**The Goal:** Before the AI can help, it needs to know what the user is working on. This phase builds the system's "eyes" and the main user dashboard.

* **Step 1: The Silent Observer**
  * **What we are building:** A lightweight, invisible program that runs in the background of the computer.
  * **What it does:** It quietly takes notes on what application or document the user has open (like "Marketing Plan - Word" or "Website Code"). It uses almost no computer power and respects privacy by only logging the titles of active windows, not taking heavy screenshots.
* **Step 2: The User Dashboard**
  * **What we are building:** A sleek, professional, dark-themed app interface on the computer.
  * **What it does:** This is where the user interacts with the assistant. It shows a live feed of what the assistant is currently "seeing" so the user always knows what is being tracked.

---

## 🧠 Phase 2: The Brain & Memory (Your Personal Expert)
**The Goal:** Give the assistant the intelligence to answer questions and the memory to remember past work, all without sending sensitive company data to the internet.

* **Step 1: Adding the AI Brain**
  * **What we are building:** We will install a smart, free AI directly onto the computer.
  * **What it does:** The user can ask the assistant questions in the dashboard. Because the AI lives on the computer, it is completely private and lightning-fast.
* **Step 2: Screen Awareness**
  * **What we are building:** A quick-action button. 
  * **What it does:** If a user is stuck, they can highlight their work, press a button, and the assistant will immediately offer advice based exactly on what is on the screen. It acts like an expert looking over your shoulder.
* **Step 3: Long-Term Memory**
  * **What we are building:** A smart digital filing cabinet.
  * **What it does:** As the user works, the assistant remembers the context of the day. Later, the user can ask, *"What was I working on yesterday afternoon?"* and the assistant will perfectly recall the past work, making picking up old tasks much easier.

---

## 🌐 Phase 3: Team Collaboration (Connecting the Dots)
**The Goal:** Transform the assistant from a solo tool into a team-wide communication network.

* **Step 1: The Central Secure Hub**
  * **What we are building:** A secure, central server that acts as a traffic director for the whole team.
  * **What it does:** It ensures that all team members are securely logged in and that no one outside the team can access the communication network.
* **Step 2: Instant Team Pinging**
  * **What we are building:** A real-time messaging pipeline between different assistants.
  * **What it does:** If User A has a question for User B, User A asks their assistant. The assistant instantly sends a subtle, non-distracting popup directly to User B's screen. It includes the question *and* the exact context of what User A is struggling with, drastically reducing communication confusion.

---

## 📈 Phase 4: Automation (Eliminating Paperwork)
**The Goal:** Save time by having the AI handle the tedious task of writing daily work reports.

* **Step 1: The Background Organizer**
  * **What we are building:** A heavy-duty organizer on the central server that can handle a lot of information at once without slowing down anyone's computer.
  * **What it does:** It manages the flow of data so that when everyone logs off at 5:00 PM, the system doesn't crash.
* **Step 2: Automated Daily Summaries**
  * **What we are building:** An automatic report generator.
  * **What it does:** At the end of the day, the AI gathers all its notes on what the user worked on and drafts a clean, professional "Daily Summary" report. 
  * **The Result:** The user simply reads the draft, clicks **"Approve,"** and it is automatically sent to their manager. No more wasting 30 minutes trying to remember what was accomplished that day.

---

## 🏁 Final Project Review
By the end of these four phases, the team will have a highly secure assistant that:
1. Privately knows what you are working on.
2. Acts as an instant expert for problem-solving.
3. Remembers your past work perfectly.
4. Allows you to seamlessly communicate complex problems to teammates.
5. Writes your daily status reports for you.

---

# 🗺️ Nexus AI: Step-by-Step Developer Execution Roadmap

---


### 🔍 Phase 3: Deep Context Injection (The "Cursor" Core Engine)

*Status: Next Immediate Target*

* **Step 3.1: Active File Source Reader**
* **What it does:** Upgrades your background Python daemon. When the window tracker identifies an active app state like `views.py - VS Code`, the daemon parses the filename (`views.py`) and matches it against your active workspace directory. It extracts the raw code lines directly from the file disk location.


* **Step 3.2: File-Change Event Hook (`watchdog`)**
* **What it does:** Hooks into your local file system using the Python `watchdog` library. It monitors file save commands (`Ctrl+S`). It only reads the file contents when a save occurs to eliminate constant hard drive read/write operations.


* **Step 3.3: Automated Prompt Context Mixer**
* **What it does:** Intercepts your chat input messages. Before transmitting the query to Ollama, it appends the current file context string inside a hidden system block: *"You are reviewing the developer's workspace code. Current file content: [Raw Code String]."* This allows the AI to diagnose errors or complete missing sections without you pasting your files.



---

### 🎙️ Phase 4: Voice-Activated Interaction System (Audio Pipelines)

*Status: Ready to Design*

* **Step 4.1: Local Microphone Stream Capturer**
* **What it does:** Uses Python libraries (`sounddevice` and `numpy`) or the React Web Audio API to record spoken developer queries when a physical hotkey event fires, encoding the audio frames natively on the fly.


* **Step 4.2: Local Speech-to-Text Processing (`Faster-Whisper`)**
* **What it does:** Routes captured audio signals through a C++ optimized local translation model engine (`Faster-Whisper`) on your CPU, transforming voice vibrations into text phrases instantly inside your application state.


* **Step 4.3: Local Text-to-Speech Output Pipeline (`Piper TTS`)**
* **What it does:** Captures the text string streaming from your Ollama coding model and routes it directly into a high-performance local phoneme model engine (**Piper** using `.onnx` models). It pronounces the code fixes aloud via your speakers as the text appears on the screen.

---
           
### 🌐 Phase 5: Local Memory Vault (Long-Term Vector RAG)

*Status: Advanced Local Layer*

* **Step 5.1: Vector Embedding Tokenization (`nomic-embed-text`)**
* **What it does:** Periodically processes text logging sequences generated throughout your work session, converting descriptive English lines into multi-dimensional mathematical coordinate spaces.


* **Step 5.2: Local Database Memory Indexed Management (`chromadb`)**
* **What it does:** Persists those coordinate strings inside a local vector database profile collection on your drive.


* **Step 5.3: Semantic Search Retrieval Loop**
* **What it does:** Lets you ask natural-language historical questions like *"What file details was I checking before lunch?"*. The system searches ChromaDB coordinates, extracts relevant chronological log fragments, and passes them to the model so it can answer with complete historical accuracy.



---

### 🏢 Phase 6: Central Team Sync Hub (Production Django & WebSockets)

*Status: Multi-User Scaling*

* **Step 6.1: Relational Security & Multi-Tenant Database Core (DRF + JWT)**
* **What it does:** Builds a production-grade Django web server backed by an enterprise PostgreSQL storage system. Protects administrative layers using secure JSON Web Token authentication (`django-rest-framework-simplejwt`) and restricts viewing privileges using explicit Role-Based Access Control logic (`IsHRorAdmin`).


* **Step 6.2: Real-Time Communication Sockets (Django Channels + Redis)**
* **What it does:** Manages live full-duplex socket connections between your desktop clients using Django Channels and a Redis server container. Allows your system to broadcast lightweight status updates, project names, or team support pings instantly without polling delays.


* **Step 6.3: Asynchronous Scalable Automation Engine (Celery Workers)**
* **What it does:** Offloads intense data processes from your HTTP request handler loops into dedicated, non-blocking Celery processing workers. Runs automated scheduled routines at the end of the day to compile your local logs, generate comprehensive markdown project recaps using local LLMs, and synchronize approved updates securely with your team management profile dashboard.


---

## 🏁 Final Project Verification Matrix

Before declaring the entire project production-ready, verify this full end-to-end integration loop:

1. Open your code editor and intentionally write broken code.
2. Use the local hotkey to ask **SyncJarvis** for assistance. The AI must instantly provide a solution based on your exact open file context.
3. Use the chat function to ask the AI: *"What did I do 2 hours ago?"* The app should query ChromaDB and summarize your history accurately.
4. Ping a team member using the system UI to get their input on your current code.
5. At the end of the day, review your automated Markdown work report summary, click **Approve**, and verify it appears on your central manager dashboard securely.

---

# 🚀 Nexus AI: Phase 6 to 9 Development Roadmap

## Cloud Migration & Collaborative Team Workspace

---

## 🔒 Baseline Security Strategy: The "Invite Key" Authentication

To handle data isolation without the overhead of heavy registration databases during testing, every WebSocket payload sent from the Tauri client to the server will now append an `auth` header package:

```json
{
  "auth": {
    "invite_key": "nexus_key_44bB",
    "username": "friend_a"
  }
}

```

The FastAPI backend validates this against an in-memory or configuration file directory before spinning up separate dynamic structures.

---

## 🏗️ Detailed Phase Definitions

### Phase 6: Core Client-Server Splitting & Rust "Spy" Architecture (Step 1 & 2)

**Developer Objective:** Remove hardware dependencies from the local Python engine and port all environmental "sensor capture" mechanisms into the client-side Tauri shell.

* **Phase 6.1: Rust Native File & Window Watcher (The Client "Eyes")**
* Integrate the `notify` or `tauri-plugin-fs-watch` crate into `src-tauri`. Configure it to watch the user's active workspaces. When a file write/save triggers, the Rust kernel reads the text payload.
* Integrate a cross-platform active window tracking hook (using crates like `active-win-pos-rs` or native X11/Wayland bindings).
* Set up a background loop executing every $1000\text{ms}$ that emits a non-blocking Tauri event payload (`nexus://os-context`) to the React UI layer.


* **Phase 6.2: Web Audio API Integration (The Client "Ears")**
* Deprecate Python `sounddevice`. Implement standard browser audio recording using `navigator.mediaDevices.getUserMedia()` inside the React app.
* Configure a `ScriptProcessorNode` or `AudioWorklet` to capture audio input downsampled to $16\text{kHz}$ mono PCM stream blocks, ready for real-time WebSocket transport.



---

### Phase 7: Multi-Tenant WebSocket Pipeline & Dynamic Memory Isolation (Step 3 & 4)

**Developer Objective:** Build the secure central communication nexus on your Host PC capable of dividing database spaces dynamically per incoming connection key.

* **Phase 7.1: FastAPI Stateful Connection Manager**
* Develop a Python FastAPI WebSocket routing application exposed via `ngrok`.
* Maintain an active connections registry object mapped inside the memory cache:
```python
ACTIVE_CONNECTIONS = {
    "friend_a": {"websocket": ws_object, "status": "active"},
    "friend_b": {"websocket": ws_object, "status": "active"}
}

```




* **Phase 7.2: Dynamic ChromaDB Multi-Tenancy Engine**
* Refactor the vector database initialization logic. When a user connects and validates their invite key, dynamically spawn separate collections bounded by their username:
```python
user_codebase = chroma_client.get_or_create_collection(name=f"{username}_codebase_vault")
user_timeline = chroma_client.get_or_create_collection(name=f"{username}_activity_vault")

```


* Route file synchronization payloads coming from Friend A exclusively to `friend_a_codebase_vault`.

You should add the PostgreSQL migration directly into **Phase 7**.

Because Phase 6 is focused entirely on the **Client/Frontend** (converting the tracker into Rust and Tauri), your database doesn't change yet. **Phase 7** is where we rebuild your **Server/Backend**, making it the absolute perfect spot to rip out SQLite3 and hook up your new PostgreSQL engine.

Here is exactly how your updated roadmap looks with the Postgres migration injected as **Phase 7.3**, using your database password (`Postgres@1011`).

---

* **Phase 7.3: PostgreSQL Enterprise Database Migration Core 🆕**
* Deprecate the `sqlite3` tracking file module.
* Establish a centralized engine wrapper utilizing your custom pgAdmin credentials:
```python
# database.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DATABASE_URL = "postgresql://nexus_admin:Postgres@1011@localhost:5432/nexus_cloud" # -> (it is example)
#database_name = postgres
#owner = postgres
# password = Postgres@1011 - make sure not nay error by @ character in pasword

engine = create_engine(DATABASE_URL, pool_size=20, max_overflow=0)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

```

---

### Phase 8: Inter-Agent Team Communication Framework (The Collaborative Core)

**Developer Objective:** Build the routing layer that allows User A's Agent to pass queries natively onto User B's UI desktop space over the WebSocket broker.

* **Phase 8.1: NLP Intent Parsing for Team Interception**
* Update the System Prompt for your main LLM Core (`qwen2.5-coder`). Instruct the AI to watch for routing keywords like *"Ask [username] [question]"*.
* If a routing intent is detected, the LLM must abort standard TTS generation and instead output a structured routing contract:
```json
{
  "control_action": "AGENT_INTERCEPT_ROUTE",
  "target_user": "friend_a",
  "origin_user": "empiric",
  "query_text": "Why is the database pool connection failing in your latest sync?"
}

```




* **Phase 8.2: Target UI Prompting & Core Action States**
* When the server receives an `AGENT_INTERCEPT_ROUTE` schema, it targets the specific connection matching `target_user` and pushes a live structural payload.
* The floating Tauri UI agent on Friend A's computer transitions into a **High-Priority Attention pulsing state** and renders an overlaid interactive prompt window showing the origin user's question along with **three specific execution branches**:



| Option Node | UI Component Triggered | Execution Flow Protocol |
| --- | --- | --- |
| **1. Let AI Answer** | Action Button Click | Client triggers a signal back to the Host server. The server reads `friend_a_codebase_vault`, runs a RAG lookup on Friend A's local data, answers it using Ollama, and broadcasts the response back to User A's audio pipeline. |
| **2. Quick Chat** | Action Button Click | Automatically flashes a sidebar conversation window on both client apps, locking them into a direct P2P WebSocket text channel. |
| **3. Write Answer** | Inline Text Field | Opens a native custom textarea block directly under the agent orb. Friend A types their thoughts. On hit enter, text is piped to the server, injected directly into User A's screen, and read aloud using `piper-tts`. |

---

### Phase 9: Separation of Communication Matrices (Direct vs. Team Channels)

**Developer Objective:** Introduce a secure, localized chat matrix allowing clean switching between public channel broadcasting and sandboxed isolated peer interactions.

* **Phase 9.1: Message Broker Topology**
* Update your server-side payload routing definitions to explicitly distinguish between message targets using an explicit enum scheme (`peer_to_peer` | `broadcast_group`).


* **Phase 9.2: Group Channel Sync**
* Construct global chat rooms (`#general`, `#engineering`). Any data package routed with a room channel designation is immediately echoed to all authenticated clients currently registered in the active system loops, bypassing local RAG embedding matrices entirely to keep the line free for pure human messaging.



---

### single Relevant Question

To begin building Phase 6, do you want the Tauri client to watch your entire home project path (`~/Projects/Nexus AI/`), or should we configure the Rust file watcher to only listen to a specific project directory passed dynamically by the user?