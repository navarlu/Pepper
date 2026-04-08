## Operator panel:
- Add clear button for the conv hisory
- resrart session takes ages
## Agent:

- Knowladge
    - contacts
    - ways to rooms (fill the rest)
    - calendar

- Animation
    - make them muted

- Make her say random stuff ocasionaly

# Pepper
    
- eyes indicating search

## Tablet

- Flash the imporant stuff
- Clear history on session end


## Thesis:

- Declaration misto specifications
- Explain RAG
    - Data preparation
        - Finding links
        - Sracping web


## Look at:

- cospeach

## Experiment:

- create the paper with info
- find the stand
- discuss experiment with Json
- log system of interactions

## Repo:

- create main from RPI branch

## Bugs:

- Pepper says hello on the startup
- Pepper is loud on startup

Log system

Finetune the local agent

Quick autostart for openai


when restart agent it will not dispatch automaticly
remove memory warning from agent


and do you remember the test we did, can you try complelty sepearatly from the project try to use Qwe with 3 simple dummy tool I wanted if its realy hard limit for 2 tools, check with documentation.

Try
vllm serve Qwen/Qwen3-8B-FP8 \
  --host 127.0.0.1 --port 8000 \
  --enable-auto-tool-choice --tool-call-parser hermes \
  --max-model-len 8192


vllm serve Qwen/Qwen3.5-9B \
  --host 127.0.0.1 --port 8000 \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder \
  --max-model-len 8192 \
  --quantization fp8 \
  --gpu-memory-utilization 0.9

DeepSeek-R1 7B distilled
Google Gemma 3 9B


Text Input appearing twice
add animation to while waiting for query
Bigger TTS and STT?

Live behaviour
openAI slow 