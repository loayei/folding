import os
import pandas as pd
import numpy as np
import bittensor as bt
from typing import List, Dict
from collections import defaultdict

from folding.validators.protein import Protein
from folding.utils.data import DataExtractor
from folding.protocol import FoldingSynapse
from folding.rewards.reward import RewardEvent
from folding.rewards.energy import EnergyRewardModel
from folding.rewards.rmsd import RMSDRewardModel


def get_energies(protein: Protein, responses: List[FoldingSynapse], uids: List[int]):
    """Takes all the data from reponse synapses, applies the reward pipeline, and aggregates the rewards
    into a single torch.FloatTensor. Also aggregates the RMSDs for logging.

    Returns:
        tuple:
            torch.FloatTensor: A tensor of rewards for each miner.
            torch.FloatTensor: A tensor of RMSDs for each miner.
    """
    event = defaultdict(list)
    energies = np.zeros(len(uids))
    for i, (uid, resp) in enumerate(zip(uids, responses)):
        # Ensures that the md_outputs from the miners are parsed correctly
        try:
            if not protein.process_md_output(
                md_output=resp.md_output, hotkey=resp.axon.hotkey
            ):
                continue

            if resp.dendrite.status_code != 200:
                bt.logging.info(
                    f"uid {uid} responded with status code {resp.dendrite.status_code}"
                )
                continue
            energy = protein.get_energy(data_type="Potential")
            rmsd = protein.get_rmsd()

            is_valid, checked_energy = protein.is_run_valid(
                energies[i], resp.axon.hotkey
            )
            energies[i] = energy if is_valid else 0

            event["is_valid"].append(is_valid)
            event["checked_energy"].append(checked_energy)
            event["reported_energy"].append(energy)
            event["rmsds"].append(rmsd.iloc[-1]["rmsd"])
            energies[i] = energy.iloc[-1]["energy"]

        except Exception as E:
            # If any of the above methods have an error, we will catch here.
            bt.logging.error(
                f"Failed to parse miner data for uid {uid} with error: {E}"
            )
            continue

    return energies, event
