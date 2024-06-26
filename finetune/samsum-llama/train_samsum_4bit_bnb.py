import argparse

# Set up the argument parser
parser = argparse.ArgumentParser(description='Python script to work with models')
parser.add_argument('--model_name', type=str, help='Name of the model', required=True)
parser.add_argument('--adapter', type=str, help='Path to store adapter weight', required=True)
parser.add_argument('--mbatch_size', type=int, help='mbatch size for training', required=True)
parser.add_argument('--seed', type=int, help='model seed number', required=True)
parser.add_argument('--repo_name', type=str, help='HF model name', required=True)


# Parse the arguments
args = parser.parse_args()

# Use the command line arguments in your script
print('Model Name:', args.model_name)
print('Adapter Path: ', args.adapter)
print('Seed: ', args.seed)
print('mbatch_size: ', args.mbatch_size)


import random
import json
import os

# import wandb
import torch
import numpy as np
import bitsandbytes as bnb
from tqdm import tqdm
import transformers
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM, AutoModelForCausalLM, DataCollatorForTokenClassification, DataCollatorForSeq2Seq
from transformers import Trainer, TrainingArguments, logging, TrainerState, TrainerControl, BitsAndBytesConfig
from transformers.trainer_utils import PREFIX_CHECKPOINT_DIR
from peft import get_peft_model, LoraConfig, prepare_model_for_int8_training
from datasets import load_dataset

from utils import fix_model, fix_tokenizer, set_random_seed
from data import InstructDataset


os.environ["WANDB_DISABLED"] = "true"
os.environ["TOKENIZERS_PARALLELISM"] = "false"



checkpoint = None
seed = args.seed
train_sample_rate = 1.0
val_sample_rate = 1.0
local_rank = 0
output_dir = args.adapter

set_random_seed(seed)
logging.set_verbosity_info()




device_map = "auto"
world_size = int(os.environ.get("WORLD_SIZE", 1))
ddp = world_size != 1
if ddp:
    device_map = {"": int(os.environ.get("LOCAL_RANK") or 0)}
    gradient_accumulation_steps = gradient_accumulation_steps // world_size


### Training Configuration
MICRO_BATCH_SIZE = args.mbatch_size  # this could actually be 5 but i like powers of 2
BATCH_SIZE = 128
GRADIENT_ACCUMULATION_STEPS = BATCH_SIZE // MICRO_BATCH_SIZE
EPOCHS = 3  # we don't need 3 tbh
LEARNING_RATE = 1e-3  # the Karpathy constant
CUTOFF_LEN = 128  # 128 accounts for about 95% of the data
LORA_R = 8
LORA_ALPHA = 16
LORA_DROPOUT = 0.05
VAL_SET_SIZE= 2000

def preprocess_logits_for_metrics(logits, labels):
    """
    Original Trainer may have a memory leak.
    This is a workaround to avoid storing too many tensors that are not needed.
    """
    pred_ids = torch.argmax(logits[0], dim=-1)
    return pred_ids, labels

trainer_config = transformers.TrainingArguments(
    per_device_train_batch_size = MICRO_BATCH_SIZE,
    per_device_eval_batch_size = MICRO_BATCH_SIZE,
    gradient_accumulation_steps=GRADIENT_ACCUMULATION_STEPS,
    warmup_ratio=0.06,
    #num_train_epochs=3,
    max_steps = 350,
    learning_rate=LEARNING_RATE,
    lr_scheduler_type = "cosine", ## LoRA original paper uses linear
    fp16=True,
    logging_steps=50,
    evaluation_strategy="steps",
    logging_strategy="steps",
    save_strategy="steps",
    eval_steps=50,
    save_steps=50,
    # report_to=report_to,
    output_dir=output_dir,
    optim = "adamw_torch",
    torch_compile = False,
    save_total_limit=2,
    load_best_model_at_end=False,
    ddp_find_unused_parameters=False if ddp else None,
)


# ### Apply LoRA
#
# Here comes the magic with `peft`! Let's load a `PeftModel` and specify that we are going to use low-rank adapters (LoRA) using `get_peft_model` utility function from `peft`.

target_modules = None
target_modules = ['q_proj', 'v_proj'] # edit with your desired target modules

lora_config = LoraConfig(
    r=8, lora_alpha=32, target_modules=target_modules, lora_dropout=0.1, bias="none", task_type="CAUSAL_LM"
)


training_args = trainer_config


model_name = args.model_name

tokenizer = AutoTokenizer.from_pretrained(model_name, use_fast=False)
tokenizer = fix_tokenizer(tokenizer)
# tokenizer.save_pretrained(output_dir)

dataset = load_dataset('samsum')
train_records = dataset['train']
val_records = dataset['test']
#random.shuffle(train_records)
print("train_record[0]: ",train_records[0])

## Config for llama 65-b
model_type = "causal"
templates_path = "llama_lora_samsum.json"
only_target_loss = False
mode = "instruct"


if mode == "instruct":
    max_source_tokens_count = 255 # Changed depending on the dataset
    max_target_tokens_count = 50
    target_field = "summary"
    source_field = "" #does not matter. (original alpaca-lora paper has additional "input" alongside instruction: instruction-input-output vs. instruction-response)

    train_dataset = InstructDataset(
        train_records,
        tokenizer,
        max_source_tokens_count=max_source_tokens_count,
        max_target_tokens_count=max_target_tokens_count,
        sample_rate=train_sample_rate,
        input_type=model_type,
        templates_path=templates_path,
        target_field=target_field,
        source_field=source_field,
        only_target_loss=only_target_loss
    )

    val_dataset = InstructDataset(
        val_records,
        tokenizer,
        max_source_tokens_count=max_source_tokens_count,
        max_target_tokens_count=max_target_tokens_count,
        sample_rate=val_sample_rate,
        input_type=model_type,
        templates_path=templates_path,
        target_field=target_field,
        source_field=source_field,
        only_target_loss=only_target_loss
    )

else:
    assert False

if "seq2seq" in model_type:
    data_collator = DataCollatorForSeq2Seq(tokenizer, pad_to_multiple_of=8)
else:
    data_collator = DataCollatorForTokenClassification(tokenizer, pad_to_multiple_of=8)

print("INPUT_IDS")
print(data_collator([train_dataset[0], train_dataset[1]])["input_ids"][0])
print("MASK")
print(data_collator([train_dataset[0], train_dataset[1]])["attention_mask"][0])
print("LABELS")
print(data_collator([train_dataset[0], train_dataset[1]])["labels"][0])


model_types = {
    "causal": AutoModelForCausalLM,
    "seq2seq": AutoModelForSeq2SeqLM
}
## Decide whether to laod in 8-bit
load_in_8bit = False
load_in_4bit = True
if load_in_8bit:
    assert not load_in_4bit
    model = model_types[model_type].from_pretrained(
        model_name,
        load_in_8bit=True,
        device_map=device_map
    )
    model = fix_model(model, tokenizer, use_resize=False)
    model = prepare_model_for_int8_training(model)
elif load_in_4bit:
    assert not load_in_8bit
    # use_bf16 = trainer_config.get("bf16", False)
    use_bf16 = getattr(trainer_config, "bf16", False)
    compute_dtype = torch.bfloat16 if use_bf16 else torch.float16
    model = model_types[model_type].from_pretrained(
        model_name,
        load_in_4bit=True,
        device_map=device_map,
        quantization_config=BitsAndBytesConfig(
            load_in_4bit=True,
            llm_int8_threshold=6.0,
            llm_int8_has_fp16_weight=False,
            bnb_4bit_compute_dtype=compute_dtype,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type="nf4"
        ),
        torch_dtype=torch.bfloat16 if use_bf16 else torch.float32
    )
    model = fix_model(model, tokenizer, use_resize=False)
    model = prepare_model_for_int8_training(model)
else:
    model = model_types[model_type].from_pretrained(model_name)
    model = fix_model(model, tokenizer)

# Default model generation params
model.config.num_beams = 5
if mode == "instruct":
    max_tokens_count = max_target_tokens_count + max_source_tokens_count + 1
model.config.max_length = max_tokens_count if model_type == "causal" else max_target_tokens_count

if not ddp and torch.cuda.device_count() > 1:
    model.is_parallelizable = True
    model.model_parallel = True


model = get_peft_model(model, lora_config)

trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=train_dataset,
    eval_dataset=val_dataset,
    data_collator=data_collator,
    preprocess_logits_for_metrics = preprocess_logits_for_metrics,
)

# with wandb.init(project="llama_ft_samsum", name="llama finetuning run") as run: ## changed the name don't forget
checkpoint_dir = output_dir
if os.path.exists(checkpoint_dir) and os.listdir(checkpoint_dir):
    trainer.train(resume_from_checkpoint=True)
else:
    trainer.train()
model.save_pretrained(output_dir)

trainer.model.push_to_hub(args.repo_name)