import jax
import flax

import jax.numpy as jnp
from flax import traverse_util
from flax.training import train_state
import optax
from typing import Callable

from transformers import FlaxWav2Vec2ForCTC

model_id = "facebook/wav2vec2-base"
model = FlaxWav2Vec2ForCTC.from_pretrained(model_id)

from speech_jax import DataLoader, TrainerConfig, Trainer

import dataclasses
from tqdm.auto import tqdm

from functools import partial


from typing import List, Dict, Any, Optional
from transformers import Wav2Vec2FeatureExtractor, Wav2Vec2CTCTokenizer

@flax.struct.dataclass
class TrainState(train_state.TrainState):
    loss_fn: Callable = flax.struct.field(pytree_node=False)


def create_tx(lr, weight_decay):
    def weight_decay_mask(params):
        params = traverse_util.flatten_dict(params)
        mask = {k: (v[-1] != "bias" and v[-2:] != ("LayerNorm", "scale")) for k, v in params.items()}
        return traverse_util.unflatten_dict(mask)
    tx = optax.adamw(learning_rate=lr, weight_decay=weight_decay, mask=weight_decay_mask)
    return tx


state = TrainState.create(
    apply_fn=model.__call__,
    params=model.params,
    tx=create_tx(1e-4, 1e-4),
    loss_fn=optax.ctc_loss,
)


@partial(jax.pmap, axis_name="batch")
def training_step(batch, state, drp_rng: jnp.DeviceArray):
    new_drp_rng, drp_rng = jax.random.split(drp_rng, num=2)

    def loss_fn(params):
        targets = batch.pop("targets")
        outputs = state.apply({"params": params}, **batch, dropout_rng=drp_rng, train=True)
        return state.loss_fn(targets, outputs)

    grad_fn = jax.value_and_grad(loss_fn)
    loss, grads = grad_fn(state.params)
    loss = jax.lax.pmean(loss, axis_name="batch")
    grads = jax.lax.pmean(grads, axis_name="batch")

    loss = state.apply_gradient(grads)

    return loss, new_drp_rng


@partial(jax.pmap, axis_name="batch")
def validation_step(batch, state):
    targets = batch.pop("targets")
    outputs = state.apply({"params": state.params}, **batch, train=False)
    loss = state.loss_fn(targets, outputs)
    loss = jax.lax.pmean(loss, axis_name="batch")
    return loss

@dataclasses.dataclass
class DataCollator:
    feature_extractor: Wav2Vec2FeatureExtractor
    tokenizer: Wav2Vec2CTCTokenizer
    audio_max_len: Optional[int] = None
    text_max_len: Optional[int] = None

    def __call__(self, batch: List[Dict[str, Any]]):
        audio = [sample["audio"]["array"] for sample in batch]
        text = [sample["text"] for sample in batch]

        # TODO: explore other padding options in JAX (special dynamic padding?)
        audio = self.feature_extractor(audio, padding="max_length", max_length=self.audio_max_len, truncation=True, return_tensors="np")
        text = self.tokenizer(text, max_length=self.text_max_len, truncation=True, padding="max_length", return_tensors="np")
        return audio, text


feature_extractor = Wav2Vec2FeatureExtractor.from_pretrained(model_id)
tokenizer = Wav2Vec2CTCTokenizer.from_pretrained(model_id)
collate_fn = DataCollator(feature_extractor, tokenizer, audio_max_len=256000, text_max_len=16)

trainer_config = TrainerConfig(
    max_epochs=30,
    train_batch_size_per_device=2,
    eval_batch_size_per_device=2,
    wandb_project_name="speech-JAX",
)

trainer = Trainer(
    config=trainer_config,
    datacollator=collate_fn,
    training_step=training_step,
    validation_step=validation_step,
    state=state,
)


from datasets import interleave_datasets, load_dataset
train_data = [
    load_dataset("librispeech_asr", "clean", split="train.100", streaming=True),
    load_dataset("librispeech_asr", "clean", split="train.360", streaming=True),
    load_dataset("librispeech_asr", "other", split="train.500", streaming=True),
]
train_data = interleave_datasets(train_data)
val_data = load_dataset("librispeech_asr", "clean", split="validation", streaming=True)


dataloader = DataLoader(train_data, batch_size=4, collate_fn=collate_fn)

for batch in tqdm(dataloader):
    print(batch)
    break
