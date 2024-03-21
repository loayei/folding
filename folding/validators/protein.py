import os
import sys
import re
import tqdm
import pickle
import random
import hashlib
import requests

import bittensor as bt

from dataclasses import dataclass


# root level directory for the project (I HATE THIS)
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PDB_PATH = os.path.join(ROOT_DIR, "./pdb_ids.pkl")
if not os.path.exists(PDB_PATH):
    raise ValueError(
        f"Required Pdb file {PDB_PATH!r} was not found. Run `python scripts/gather_pdbs.py` first."
    )

with open(PDB_PATH, "rb") as f:
    PDB_IDS = pickle.load(f)


@dataclass
class Protein:
    @property
    def name(self):
        return self.protein_pdb.split(".")[0]

    def __init__(self, pdb_id=None, ff="charmm27", box="dodecahedron", max_steps=None):
        # can either be local file path or a url to download
        if pdb_id is None:
            pdb_id = self.select_random_pdb_id()
            bt.logging.success(f"Selected random pdb id: {pdb_id!r}")

        self.pdb_id = (
            pdb_id.lower()
        )  # pdb_id is insensitive to capitalization so we convert to lowercase
        self.ff = ff
        self.box = box

        self.pdb_file = f"{self.pdb_id}.pdb"

        self.base_directory = os.path.join(ROOT_DIR, "data")
        self.pdb_directory = os.path.join(self.base_directory, self.pdb_id)

        bt.logging.info(f"\nChanging output directory to {self.pdb_directory}")

        # if directory doesn't exist, download the pdb file and save it to the directory
        if not os.path.exists(self.pdb_directory):
            os.makedirs(self.pdb_directory)

        if not os.path.exists(os.path.join(self.pdb_directory, self.pdb_file)):
            bt.logging.info(
                f"\n⏰ {self.pdb_file} does not exist in repository... Downloading"
            )
            self.download_pdb()
            bt.logging.info(f"\n💬 {self.pdb_file} Downloaded!")
        else:
            bt.logging.success(
                f"PDB file {self.pdb_file} already exists in path {self.pdb_directory!r}."
            )

        self.gro_path = os.path.join(self.pdb_directory, "em.gro")
        self.topol_path = os.path.join(self.pdb_directory, "topol.top")
        mdp_files = ["nvt.mdp", "npt.mdp", "md.mdp"]
        other_files = ["em.gro", "topol.top", "posre.itp"]
        required_files = mdp_files + other_files
        missing_files = [
            filename
            for filename in required_files
            if not os.path.exists(os.path.join(self.pdb_directory, filename))
        ]

        if missing_files:
            bt.logging.warning(
                f"Essential files are missing from path {self.pdb_directory!r}: {missing_files!r}... Generating!"
            )
            self.generate_input_files()

        self.validator_directory = os.path.join(self.pdb_directory, "validator")

        self.md_inputs = {}
        for file in other_files:
            self.md_inputs[file] = open(
                os.path.join(self.validator_directory, file), "r"
            ).read()

        params_to_change = [
            "nstxout",  # Save coordinates every 0 steps (not saving standard trajectories)
            "nstvout",  # Save velocities every 0 steps
            "nstfout",  # Save forces every 0 steps
            "nstxtcout",  # Save coordinates to trajectory every 50,000 steps
            "nstenergy",  # Save energies every 50,000 steps
            "nstlog",  # Update log file every 50,000 steps
        ]

        save_frequency = 0.10  # how often to save based on the max_steps

        bt.logging.info("Extracting file content...")
        for file in mdp_files:
            filepath = os.path.join(self.validator_directory, file)
            bt.logging.info(f"Processing file {filepath}")
            content = open(filepath, "r").read()
            if max_steps is not None:
                content = re.sub(
                    "nsteps\\s+=\\s+\\d+", f"nsteps = {max_steps}", content
                )
            for param in params_to_change:
                if param in content:
                    bt.logging.info(f"Changing {param} in {file} to 1...")
                    content = re.sub(
                        f"{param}\\s+=\\s+\\d+",
                        f"{param} = {1}",
                        content,  # int(max_steps * save_frequency)
                    )

            self.md_inputs[file] = content

        self.remaining_steps = []

    def __str__(self):
        return f"Protein(pdb_id={self.pdb_id}, ff={self.ff}, box={self.box}, output_directory={self.pdb_directory})"

    def __repr__(self):
        return self.__str__()

    def check_if_directory_exists(self, output_directory):
        if not os.path.exists(output_directory):
            os.makedirs(output_directory)
            bt.logging.debug(f"Created directory {output_directory!r}")

    def select_random_pdb_id(self):
        """This function is really important as its where you select the protein you want to fold"""
        while True:
            family = random.choice(list(PDB_IDS.keys()))
            choices = PDB_IDS[family]
            if len(choices):
                return random.choice(choices)

    # Function to download PDB file
    def download_pdb(self):
        url = f"https://files.rcsb.org/download/{self.pdb_file}"
        path = os.path.join(self.pdb_directory, f"{self.pdb_file}")
        r = requests.get(url)
        if r.status_code == 200:
            with open(path, "w") as file:
                file.write(r.text)
            bt.logging.info(
                f"PDB file {self.pdb_file} downloaded successfully from {url} to path {path!r}."
            )
        else:
            bt.logging.error(
                f"Failed to download PDB file with ID {self.pdb_file} from {url}"
            )
            raise Exception(f"Failed to download PDB file with ID {self.pdb_file}.")

    # Function to generate GROMACS input files
    def generate_input_files(self):
        os.chdir(self.pdb_directory)

        # Commands to generate GROMACS input files

        suppression_command = " > /dev/null 2>&1"

        commands = [
            f"gmx pdb2gmx -f {self.pdb_file} -ff {self.ff} -o processed.gro -water spce"
            + suppression_command,  # Input the file into GROMACS and get three output files: topology, position restraint, and a post-processed structure file
            f"gmx editconf -f processed.gro -o newbox.gro -c -d 1.0 -bt {self.box}"
            + suppression_command,  # Build the "box" to run our simulation of one protein molecule
            "gmx solvate -cp newbox.gro -cs spc216.gro -o solvated.gro -p topol.top"
            + suppression_command,
            "touch ions.mdp",  # Create a file to add ions to the system
            "gmx grompp -f ions.mdp -c solvated.gro -p topol.top -o ions.tpr"
            + suppression_command,
            'echo "13" | gmx genion -s ions.tpr -o solv_ions.gro -p topol.top -pname NA -nname CL -neutral'
            + suppression_command,
        ]
        # Run the first step of the simulation
        commands += [
            f"gmx grompp -f {self.base_directory}/minim.mdp -c solv_ions.gro -p topol.top -o em.tpr"
            + suppression_command,
            "gmx mdrun -v -deffnm em" + suppression_command,  # Run energy minimization
        ]

        # strip away trailing number in forcefield name e.g charmm27 -> charmm
        ff_base = "".join([c for c in self.ff if not c.isdigit()])
        # Copy mdp template files to output directory
        commands += [
            f"cp {self.base_directory}/nvt-{ff_base}.mdp nvt.mdp",
            f"cp {self.base_directory}/npt-{ff_base}.mdp npt.mdp",
            f"cp {self.base_directory}/md-{ff_base}.mdp  md.mdp ",
        ]
        # run commands and raise exception if any of the commands fail
        for cmd in tqdm.tqdm(commands):
            bt.logging.info(f"Running GROMACS command: {cmd}")
            if os.system(cmd) != 0:
                raise Exception(
                    f"generate_input_files failed to run GROMACS command: {cmd}"
                )

        # Here we are going to change the path to a validator folder, and move ALL the files

        output_directory = os.path.join(self.pdb_directory, "validator")
        self.check_if_directory_exists(output_directory)

        # Move all files
        cmd = f'find . -maxdepth 1 -type f ! -name "*.pdb" -exec mv {{}} {output_directory}/ \;'
        bt.logging.info(f"Moving all files except pdb to {output_directory}")
        os.system(cmd)

        # We want to catch any errors that occur in the above steps and then return the error to the user
        return True

    def gro_hash(self, gro_path):
        bt.logging.info(f"Calculating hash for path {gro_path!r}")
        pattern = re.compile(r"\s*(\d+\w+)\s+(\w+\d*\s*\d+)\s+(\-?\d+\.\d+)+")

        with open(gro_path, "rb") as f:
            name, length, *lines, _ = f.readlines()
            length = int(length)
            bt.logging.info(f"{name=}, {length=}, {len(lines)=}")

        buf = ""
        for line in lines:
            line = line.decode().strip()
            match = pattern.match(line)
            if not match:
                raise Exception(f"Error parsing line in {gro_path!r}: {line!r}")
            buf += match.group(1) + match.group(2).replace(" ", "")

        return hashlib.md5(name + buf.encode()).hexdigest()

    def save_md_outputs(self, md_output: dict, hotkey: str):
        output_directory = os.path.join(self.pdb_directory, "dendrite", hotkey)
        self.check_if_directory_exists(output_directory=output_directory)

        filetypes = {}
        for filename, content in md_output.items():
            filetypes[filename.split(".")[-1]] = filename
            # loop over all of the output files and save to local disk
            with open(os.path.join(output_directory, filename), "wb") as f:
                f.write(content)

        return filetypes

    def reward(self, md_output: dict, hotkey: str, mode: str = "13"):
        """Calculates the free energy of the protein folding simulation
        # TODO: Each miner files should be saved in a unique directory and possibly deleted after the reward is calculated
        """

        filetypes = self.save_md_outputs(md_output=md_output, hotkey=hotkey)

        bt.logging.info(
            f"Recieved the following files from hotkey {hotkey}: {list(filetypes.keys())}"
        )
        edr = filetypes.get("edr")
        if not edr:
            bt.logging.error(
                f"No .edr file found in md_output ({list(md_output.keys())}), so reward is zero!"
            )
            return 0

        gro = filetypes.get("gro")
        if not gro:
            bt.logging.error(
                f"No .gro file found in md_output ({list(md_output.keys())}), so reward is zero!"
            )
            return 0

        gro_path = os.path.join(resp_dir, gro)
        if self.gro_hash(self.gro_path) != self.gro_hash(gro_path):
            bt.logging.error(
                f"The hash for .gro file from hotkey {hotkey} is incorrect, so reward is zero!"
            )
            return 0
        bt.logging.success(f"The hash for .gro file is correct!")

        os.chdir(resp_dir)
        edr_path = os.path.join(resp_dir, edr)
        commands = [f'echo "13"  | gmx energy -f {edr} -o free_energy.xvg']

        # TODO: we still need to check that the following commands are run successfully
        for cmd in tqdm.tqdm(commands):
            bt.logging.info(f"Running GROMACS command: {cmd}")
            if os.system(cmd) != 0:
                raise Exception(f"reward failed to run GROMACS command: {cmd}")

        energy_path = os.path.join(resp_dir, "free_energy.xvg")
        free_energy = self.get_average_free_energy(energy_path)
        bt.logging.success(
            f"Free energy of protein folding simulation is {free_energy}"
        )

        # return the negative of the free energy so that larger is better
        return -free_energy

    # Function to read the .xvg file and compute the average free energy
    def get_average_free_energy(self, filename):
        # Read the file, skip the header lines that start with '@' and '&'
        bt.logging.info(f"Calculating average free energy from file {filename!r}")
        with open(filename) as f:
            last_line = f.readlines()[-1]

        # The energy values are typically in the second column
        last_energy = last_line.split()[-1]

        return float(last_energy)
