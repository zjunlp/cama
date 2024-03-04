import os
import sys
from typing import List
import fire
import torch
import transformers
from datasets import load_dataset

"""
Unused imports:
import torch.nn as nn
import bitsandbytes as bnb
"""

from peft import (
    LoraConfig,
    get_peft_model,
    get_peft_model_state_dict,
    prepare_model_for_kbit_training,
    set_peft_model_state_dict,
    TaskType,
)
from transformers import AutoModelForCausalLM, AutoTokenizer

from utils.prompter import Prompter

""" 
## Notes
    We follow official finetuning script. For Qwen, target modules are set to:
    
        ["c_attn", "c_proj", "w1", "w2"]
    
    For ChatGLM3, target modules are set to:

        ["query_key_value"]
    
    PEFT will use the string method `endswith` to match the target modules.
"""
LABEL_PAD_TOKEN_ID = -100

def train(
        # model/data params
        base_model: str = "",  # the only required argument
        data_path: str = "./data/",
        output_dir: str = "./checkpoint",
        # training hyperparams
        batch_size: int = 128,
        micro_batch_size: int = 4,
        num_epochs: int = 3,
        learning_rate: float = 3e-4,
        cutoff_len: int = 512,
        val_set_ratio: float = 0.2,
        # lora hyperparams
        lora_r: int = 16,
        lora_alpha: int = 32,
        lora_dropout: float = 0.05,
        lora_target_modules: List[str] = [
            "q_proj",
            "v_proj",
            "k_proj",
            "o_proj",
            "gate_proj",
            "down_proj",
            "up_proj"
        ],
        # llm hyperparams
        train_on_inputs: bool = False,  # if False, masks out inputs in loss
        group_by_length: bool = False,  # applying dynamic padding, faster, but produces an odd training loss curve
        # wandb params
        wandb_project: str = "",
        wandb_run_name: str = "",
        wandb_watch: str = "",  # options: false | gradients | all
        wandb_log_model: str = "",  # options: false | true
        resume_from_checkpoint: str = None,  # either training checkpoint or final adapter
        prompt_template_name: str = "alpaca",  # The prompt template to use, will default to alpaca.
        # train hyperparams
        warmup_ratio=0.03,
        logging_steps=1,
        save_steps=10,
        save_total_limit=5,     # number of checkpoints to keep, useful to save memory, the best checkpoint is always kept
        eval_steps=10,
        # reproducibility
        seed=42,
):
    
    if int(os.environ.get("LOCAL_RANK", 0)) == 0:
        print(
            f"Training Alpaca-LoRA model with params:\n"
            f"base_model: {base_model}\n"
            f"data_path: {data_path}\n"
            f"output_dir: {output_dir}\n"
            f"batch_size: {batch_size}\n"
            f"micro_batch_size: {micro_batch_size}\n"
            f"num_epochs: {num_epochs}\n"
            f"learning_rate: {learning_rate}\n"
            f"cutoff_len: {cutoff_len}\n"
            f"val_set_ratio: {val_set_ratio}\n"
            f"lora_r: {lora_r}\n"
            f"lora_alpha: {lora_alpha}\n"
            f"lora_dropout: {lora_dropout}\n"
            f"lora_target_modules: {lora_target_modules}\n"
            f"train_on_inputs: {train_on_inputs}\n"
            f"group_by_length: {group_by_length}\n"
            f"wandb_project: {wandb_project}\n"
            f"wandb_run_name: {wandb_run_name}\n"
            f"wandb_watch: {wandb_watch}\n"
            f"wandb_log_model: {wandb_log_model}\n"
            f"resume_from_checkpoint: {resume_from_checkpoint or False}\n"
            f"prompt template: {prompt_template_name}\n"
        )
    assert (
        base_model
    ), "Please specify a --base_model"
    gradient_accumulation_steps = batch_size // micro_batch_size

    prompter = Prompter(prompt_template_name)

    device_map = "auto"
    world_size = int(os.environ.get("WORLD_SIZE", 1))
    ddp = world_size != 1
    if ddp:
        device_map = {"": int(os.environ.get("LOCAL_RANK") or 0)}
        gradient_accumulation_steps = gradient_accumulation_steps // world_size

    # Check if parameter passed or if set within environ
    use_wandb = len(wandb_project) > 0 or (
            "WANDB_PROJECT" in os.environ and len(os.environ["WANDB_PROJECT"]) > 0
    )

    # Only overwrite environ if wandb param passed
    if len(wandb_project) > 0:
        os.environ["WANDB_PROJECT"] = wandb_project
    if len(wandb_watch) > 0:
        os.environ["WANDB_WATCH"] = wandb_watch
    if len(wandb_log_model) > 0:
        os.environ["WANDB_LOG_MODEL"] = wandb_log_model
    # trust_remote_code=True is required to load those models which are not built-in in transformers
    model = AutoModelForCausalLM.from_pretrained(
        base_model,
        load_in_8bit=True,            # some parameters are converted to 8 bits
        torch_dtype=torch.float32,    # fp16 can save memory and speed up inference
        device_map=device_map,
        trust_remote_code=True
    )
    # use_fast=False ensures that tokenizer is not fast tokenizers 
    tokenizer = AutoTokenizer.from_pretrained(
        base_model,
        use_fast=False,
        trust_remote_code=True,
        
        )

    """
        (1) ChatGLM3 uses <unk> as padding token
        (2) Qwen uses <|endoftext|> as padding token
    """
    if tokenizer.pad_token_id is None:
        if model.config.architectures[0].startswith('QWen'):
            tokenizer.pad_token_id = tokenizer.eod_id
            # in our case, the end of text is the end of the sentence 
            tokenizer.eos_token_id = tokenizer.eod_id
            model.config.eos_token_id = tokenizer.eod_id
        else:
            tokenizer.pad_token_id = tokenizer.eos_token_id
        model.config.pad_token_id = tokenizer.pad_token_id

    def tokenize(prompt, add_eos_token=True):
        # there's probably a way to do this with the tokenizer settings
        # but again, gotta move fast
        result = tokenizer(
            prompt,
            truncation=True,
            max_length=cutoff_len,
            padding=False,
            return_tensors=None,
        )
        if (
                result["input_ids"][-1] != tokenizer.eos_token_id
                and len(result["input_ids"]) < cutoff_len
                and add_eos_token
        ):
            result["input_ids"].append(tokenizer.eos_token_id)
            result["attention_mask"].append(1)

        result["labels"] = result["input_ids"].copy()

        return result

    def generate_and_tokenize_prompt(data_point):
        full_prompt = prompter.generate_prompt(
            data_point["instruction"],
            data_point["input"],
            data_point["output"],
        )
        tokenized_full_prompt = tokenize(full_prompt)
        if not train_on_inputs:
            user_prompt = prompter.generate_prompt(
                data_point["instruction"], data_point["input"]
            )
            tokenized_user_prompt = tokenize(user_prompt, add_eos_token=False)
            user_prompt_len = len(tokenized_user_prompt["input_ids"])

            # could be sped up, probably
            tokenized_full_prompt["labels"] = [LABEL_PAD_TOKEN_ID] * user_prompt_len + tokenized_full_prompt["labels"][user_prompt_len:] 
        return tokenized_full_prompt

    model = prepare_model_for_kbit_training(model)

    # before fed into the lora layer, x is applied with dropout
    # please see https://github.com/huggingface/peft/blob/v0.7.1/src/peft/tuners/lora/layer.py#L373
    config = LoraConfig(
        r=lora_r,
        lora_alpha=lora_alpha,
        target_modules=lora_target_modules,
        lora_dropout=lora_dropout,      
        bias="none",
        task_type=TaskType.CAUSAL_LM,
    )
    model = get_peft_model(model, config)

    if resume_from_checkpoint:
        # Check the available weights and load them
        checkpoint_name = os.path.join(
            resume_from_checkpoint, "pytorch_model.bin"
        )  # Full checkpoint
        if not os.path.exists(checkpoint_name):
            checkpoint_name = os.path.join(
                resume_from_checkpoint, "adapter_model.bin"
            )  # only LoRA model - LoRA config above has to fit
            resume_from_checkpoint = (
                False  # So the trainer won't try loading its state
            )
        # The two files above have a different name depending on how they were saved, but are actually the same.
        if os.path.exists(checkpoint_name):
            print(f"Restarting from {checkpoint_name}")
            adapters_weights = torch.load(checkpoint_name)
            model = set_peft_model_state_dict(model, adapters_weights)
        else:
            print(f"Checkpoint {checkpoint_name} not found")

    model.print_trainable_parameters()  # Be more transparent about the % of trainable params.

    if data_path.endswith(".json") or data_path.endswith(".jsonl"):
        data = load_dataset("json", data_files=data_path)
        print(f"data includes: {data_path}")
    else:
        # is folder
        data_paths = []
        data_path = data_path if data_path[-1] == "/" else data_path+"/"
        for i in os.listdir(data_path):
            data_paths.append(os.path.join(data_path, i))
        print(f"data includes: {data_paths}")
        data = load_dataset(data_paths)

    if val_set_ratio > 0:
        train_val = data["train"].train_test_split(
            test_size=val_set_ratio, shuffle=True, seed=seed
        )
        train_data = (
            train_val["train"].shuffle().map(generate_and_tokenize_prompt)
        )
        val_data = (
            train_val["test"].shuffle().map(generate_and_tokenize_prompt)
        )
    else:
        train_data = data["train"].shuffle().map(generate_and_tokenize_prompt)
        val_data = None

    if not ddp and torch.cuda.device_count() > 1:
        # keeps Trainer from trying its own DataParallelism when more than 1 gpu is available
        model.is_parallelizable = True
        model.model_parallel = True
    
    trainer = transformers.Trainer(
        model=model,
        train_dataset=train_data,
        eval_dataset=val_data,
        args=transformers.TrainingArguments(
            per_device_train_batch_size=micro_batch_size,
            gradient_accumulation_steps=gradient_accumulation_steps,
            warmup_ratio=warmup_ratio,
            num_train_epochs=num_epochs,
            learning_rate=learning_rate,
            # fp16=True, 
            fp16=False,
            logging_steps=logging_steps,
            optim="adamw_torch",
            evaluation_strategy="steps" if val_set_ratio > 0 else "no",
            save_strategy="steps",
            eval_steps=eval_steps if val_set_ratio > 0 else None,
            save_steps=save_steps,
            output_dir=output_dir,
            save_total_limit=save_total_limit,
            load_best_model_at_end=True if val_set_ratio > 0 else False,
            ddp_find_unused_parameters=False if ddp else None,
            group_by_length=group_by_length,
            report_to="wandb" if use_wandb else None,
            run_name=wandb_run_name if use_wandb else None,
            seed=seed,
        ),
        data_collator=transformers.DataCollatorForSeq2Seq(
            tokenizer, 
            pad_to_multiple_of=8,  # usually fill the length of the sequence to a multiple of 8 to speed up training
            return_tensors="pt", 
            label_pad_token_id=LABEL_PAD_TOKEN_ID, 
            padding=True
        ),
    )
    # The use_cache=True option is incompatible with gradient checkpointing. Disable it for training
    model.config.use_cache = False
    
    """https://github.com/tloen/alpaca-lora/issues/609"""
    """
    # __get__ method: the frist argument is the instance of the class, the second argument is the class of the instance
    # use __get__ method to add self argument to the function
    # This is how method objects are created. When you do obj.method, the descriptor protocol is activated 
    # and the function's __get__ method is called.
    # please see https://stackoverflow.com/questions/37491487/purpose-of-get-of-a-simple-function
    old_state_dict = model.state_dict
    model.state_dict = (
        lambda self, *_, **__: get_peft_model_state_dict(
            # aftering using torch.compile, the model.state_dict() will contains extra information
            # this will cause error when loading the model
            # so we need to remove the extra information by keeping the original state_dict method
            self, old_state_dict()
        )
    ).__get__(model, type(model))

    if torch.__version__ >= "2" and sys.platform != "win32":
        # use torch.compile to speed up training 
        model = torch.compile(model)
    """
    
    # please see https://github.com/TimDettmers/bitsandbytes/issues/240
    # when we run the model on V100, it seems that the model is not running on fp16
    # so errors occur
    # with torch.autocast("cuda"):
    # only decorate the forward function with autocast
    # model.forward = torch.autocast('cuda')(model.forward).__get__(model, type(model))
    # with torch.autocast("cuda"):
    trainer.train(resume_from_checkpoint=resume_from_checkpoint) 

    # saves the adapter model and the adapter configuration files to a directory
    # so it can be re-loaded using LoraModel.from_pretrained()
    model.save_pretrained(output_dir)

    print(
        "\n If there's a warning about missing keys above, please disregard :)"
    )

if __name__ == "__main__":
    fire.Fire(train)