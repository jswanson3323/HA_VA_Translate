# Smart Router Conversation Agent

A custom Home Assistant conversation agent that adds a deterministic routing layer before invoking primary and fallback conversation agents.

This integration improves reliability for smart home control while still allowing LLM-based agents (Ollama, ChatGPT, etc.) to handle general questions.

---

## Overview

This integration introduces an intelligent routing layer into the Assist pipeline.

Instead of simply chaining agents, it:

1. Applies deterministic translation and entity resolution using entities exposed to Assist.
2. Executes Home Assistant services directly when a high-confidence match is found.
3. Falls back to the configured Primary conversation agent.
4. Falls back again to a secondary agent (LLM) if necessary.

This provides:

- Reliable device control
- Reduced dependency on fuzzy intent matching
- Clean LLM fallback
- A single unified Assist experience

---

## Routing Flow

Speech-to-Text
↓
Deterministic Translation Layer
├─ High-confidence entity match → Execute service → Return
└─ No confident match
↓
Primary Conversation Agent
├─ Success → Return
└─ Failure
↓
Fallback Conversation Agent


---

## Why This Exists

Voice pipelines often struggle with:

- Minor STT errors (e.g., "line" vs "light")
- Fuzzy entity matching
- LLM inconsistency for simple device control

This agent resolves those issues by:

- Building a live catalog of entities exposed to the conversation assistant
- Performing high-confidence matching before invoking any agent
- Only invoking LLM agents when structured intent resolution fails

Example improvements:

- “turn off the office line” → correctly resolves to `light.office_light`
- “turn on the grape room fan” → resolves to `fan.great_room_fan`
- Custom automations continue to work via the primary agent
- General questions fall back to Ollama or another LLM

---

## Features

- Deterministic entity resolution
- Primary + Fallback conversation agent support
- High-confidence matching thresholds
- Compatible with Home Assistant Assist pipelines
- Optional debug modes for tracing routing behavior

### Debug Levels

- **No Debug** – Silent routing, returns raw response
- **Low Debug** – Indicates which agent responded
- **Verbose Debug** – Shows routing decisions and fallback behavior

---

## Intended Configuration

Recommended setup:

- **Primary Agent:** Home Assistant built-in conversation agent
- **Fallback Agent:** Ollama (or another LLM-based agent)

This gives:

- Deterministic smart home control
- Support for custom automations and sentence triggers
- LLM capability only when necessary

---

## Installation

### Option 1 – HACS (Recommended)

1. Add this repository as a **Custom Repository** in HACS (Category: Integration)
2. Install the integration
3. Restart Home Assistant

### Option 2 – Manual

1. Copy the `fallback_conversation` folder into: /custom_components/
2. Restart Home Assistant

---

## Setup

1. Go to **Settings → Devices & Services**
2. Click **Add Integration**
3. Add **Smart Router Conversation Agent**
4. Configure:
- Debug level
- Primary agent
- Fallback agent
5. Go to **Settings → Voice Assistants**
6. Edit your Assistant
7. Select this agent under **Conversation Agent**

---

## Use Case

Ideal for users who:

- Run local STT (Whisper, etc.)
- Use Ollama or other LLMs
- Want consistent device control
- Want LLM fallback only when needed

---

## Contributors

Contributions are welcome.

Original concept contributors:
- @t0bst4r
- @ov1d1u
- @m50