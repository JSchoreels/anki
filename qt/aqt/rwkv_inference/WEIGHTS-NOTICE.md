# Model weights notice

The code in the anki-rwkv repository is released under the MIT License. The trained model weights
(the `weights.*.bin`, `time_head.*.bin` and `master.pth` files in the releases and on Hugging Face) are
**not** covered by the MIT License, and this project grants no license of its own to them.

- **The weights stay under Anki's terms.** The models were trained on the anki-revlogs-10k dataset
  (https://huggingface.co/datasets/open-spaced-repetition/anki-revlogs-10k): Anki review logs provided to
  the FSRS project for memory research. Training on that data does not transfer any rights in it to this
  project or to you, so treat the weights as subject to the same terms as the data. Those terms allow
  use for memory research, and by university students and individuals for their own research. Any other
  use, including shipping the weights in an application (commercial or not), needs permission from Anki,
  requested at https://anki.tenderapp.com. The weights contain no rows of the dataset, and the test
  vectors in the packages come from synthetic users.
- **Teacher model.** The models were trained with targets from the srs-benchmark RWKV checkpoint
  (https://github.com/open-spaced-repetition/srs-benchmark), which has no license yet.
- **Architecture code.** The model code comes from srs-benchmark and rwkv-anki-autoresearch (no license
  file yet) and from RWKV-LM by BlinkDL (Apache-2.0).

Whether models trained on the dataset may be redistributed and shipped in applications is an open
question upstream (https://github.com/open-spaced-repetition/srs-benchmark/issues/353). Until it is
settled, use the weights for personal and research use only. This notice will be updated once permission
is clarified. This is not legal advice.
