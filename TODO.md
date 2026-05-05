
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
- make tablet work again
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

- master -> main

## Bugs:
Open AI mode after calling tool got stuck and turned of autonomous life

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


add animation to while waiting for query
Bigger TTS and STT?
REMOVING of the agents
ADD 3 more animations
READING NUMBER


migrate to main
migrate to eth

lookup_person
return all numbers or filter just one?
add toilet instrcutions to find_directions
gym
automat
skrinky

tools o first round
animation
add imporatnt dates to knowldage
add documents for query search (maybe publications of hoffmans lab??)