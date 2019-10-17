#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pattern exploration and detection

Explore and detect patterns (loops, borders, centromeres, etc.) in Hi-C contact
maps with pattern matching.

Usage:
    chromosight detect <contact_map> [<output>] [--kernel-config FILE]
                        [--pattern=loops] [--precision=auto] [--iterations=auto]
                        [--inter] [--max-dist=auto] [--threads 1]
    chromosight generate-config <prefix> [--preset loops]

    detect: 
        performs pattern detection on a Hi-C contact map using kernel convolution
    generate-config:
        Generate pre-filled config files to use for `chromosight detect`. 
        A config consists of a JSON file describing analysis parameters for the
        detection and path pointing to kernel matrices files. Those matrices
        files are tsv files with numeric values ordered in a square dense matrix
        to use for convolution.

Arguments for detect:
    -h, --help                  Display this help message.
    --version                   Display the program's current version.
    contact_map                 The Hi-C contact map to detect patterns on, in
                                bedgraph2d or cool format. 
    output                      name of the output directory
    -I, --inter                 Enable to consider interchromosomal contacts.
    -i, --iterations auto       How many iterations to perform after the first
                                template-based pass. Auto sets an appropriate
                                value loaded from the kernel configuration
                                file. [default: auto]
    -k, --kernel-config FILE    Optionally give a path to a custom JSON kernel
                                config path. Use this to override pattern if 
                                you do not want to use one of the preset 
                                patterns.
    -m, --max-dist auto         Maximum distance from the diagonal (in base pairs)
                                at which pattern detection should operate. Auto
                                sets a value based on the kernel configuration
                                file and the signal to noise ratio. [default: auto]
    -P, --pattern loops         Which pattern to detect. This will use preset
                                configurations for the given pattern. Possible
                                values are: loops, borders, hairpin. [default: loops]
    -p, --precision auto        Precision threshold when assessing pattern
                                probability in the contact map. A lesser value
                                leads to potentially more detections, but more
                                false positives. [default: auto]
    -t, --threads 1             Number of CPUs to use in parallel. [default 1]

Arguments for generate-config:
    prefix                      Path prefix for config files. If prefix is a/b,
                                files a/b.json and a/b.1.txt will be generated.
                                If a given pattern has N kernel matrices, N txt
                                files are created they will be named a/b.[1-N].txt.
    -p, --preset loops         Generate a preset config for the given pattern.
                                Preset configs available are "loops" and 
                                "borders". [default: loops]
"""
import numpy as np
import pathlib
import sys
import json
import docopt
import multiprocessing as mp
from chromosight.version import __version__
from chromosight.utils.contacts_map import HicGenome
from chromosight.utils.io import write_results, load_kernel_config
from chromosight.utils.plotting import pattern_plot, pileup_plot
from chromosight.utils.detection import (
    pattern_detector,
    pileup_patterns,
    remove_smears,
)


def _override_kernel_config(param_name, param_value, param_type, config):
    """
    Helper function to determine if config file value should be overriden by
    user.
    """

    if param_value == "auto":
        sys.stderr.write(
            "{param_name} set to {default_val} based on config file.\n".format(
                default_val=config[param_name], param_name=param_name
            )
        )
    else:
        try:
            config[param_name] = param_type(param_value)
        except ValueError:
            raise ValueError(f'Error: {param_name} must be a {param_type} or "auto"')

    return config


def cmd_generate_config(arguments):
    # Parse command line arguments for generate_config
    prefix = arguments["<prefix>"]
    pattern = arguments["--preset"]
    arguments = docopt.docopt(__doc__, version=__version__)

    kernel_config = load_kernel_config(pattern, False)

    # Write kernel matrices to files with input prefix and replace kernels
    # by their path in config
    for mat_id, mat in enumerate(kernel_config["kernels"]):
        mat_path = f"{prefix}.{mat_id+1}.txt"
        np.savetxt(mat_path, mat)
        kernel_config["kernels"][mat_id] = mat_path

    # Write config to JSON file using prefix
    with open(f"{prefix}.json", "w") as config_handle:
        json.dump(kernel_config, config_handle, indent=4)


def _detect_sub_mat(data):
    sub = data[0][1]
    config = data[1]
    kernel = data[2]
    chrom_patterns, chrom_windows = pattern_detector(sub.contact_map, config, kernel)
    return {
        "coords": chrom_patterns,
        "windows": chrom_windows,
        "chr1": sub.chr1,
        "chr2": sub.chr2,
    }


def cmd_detect(arguments):
    # Parse command line arguments for detect
    kernel_config_path = arguments["--kernel-config"]
    interchrom = arguments["--inter"]
    iterations = arguments["--iterations"]
    mat_path = arguments["<contact_map>"]
    max_dist = arguments["--max-dist"]
    pattern = arguments["--pattern"]
    precision = arguments["--precision"]
    threads = arguments["--threads"]

    output = arguments["<output>"]
    # If output is not specified, use current directory
    if not output:
        output = pathlib.Path()
    else:
        output = pathlib.Path(output)
    output.mkdir(exist_ok=True)

    # Read a user-provided kernel config if custom is true
    # Else, load a preset kernel config for input pattern
    # Configs are JSON files containing all parameter associated with the pattern
    # They are loaded into a dictionary in the form :
    # {"max_iterations": 3, "kernels": [kernel1, kernel2, ...], ...}
    # Where each kernel is a 2D numpy array representing the pattern
    if kernel_config_path is not None:
        custom = True
        # Loading input path as config
        config_path = kernel_config_path
    else:
        custom = False
        # Will use a preset config file matching pattern name
        config_path = pattern

    ### 0: LOAD INPUT
    kernel_config = load_kernel_config(config_path, custom)

    # User can override configuration for input pattern if desired
    kernel_config = _override_kernel_config(
        "max_iterations", iterations, int, kernel_config
    )
    kernel_config = _override_kernel_config(
        "precision", precision, float, kernel_config
    )
    kernel_config = _override_kernel_config("max_dist", max_dist, int, kernel_config)

    # kernel_config = _override_kernel_config("max_dist", max_dist, int, kernel_config)
    # Make shorten max distance in case matrix is noisy
    hic_genome = HicGenome(mat_path, interchrom, kernel_config["max_dist"])

    all_pattern_coords = []

    ### 1: DETECTION ON EACH SUBMATRIX

    pool = mp.Pool(threads)
    n_sub_mats = hic_genome.sub_mats.shape[0]
    # Loop over the different kernel matrices for input pattern
    for kernel_id, kernel_matrix in enumerate(kernel_config["kernels"]):

        # Apply detection procedure to all sub matrices in parallel
        sub_mat_data = zip(
            hic_genome.sub_mats.iterrows(),
            [kernel_config for i in range(n_sub_mats)],
            [kernel_matrix for i in range(n_sub_mats)],
        )
        sub_mat_results = pool.map(_detect_sub_mat, sub_mat_data)

        # Convert coordinates from chromosome to whole genome bins
        kernel_coords = [
            hic_genome.get_full_mat_pattern(d["chr1"], d["chr2"], d["coords"])
            for d in sub_mat_results
        ]
        all_pattern_coords.append(np.concatenate(kernel_coords, axis=0))

        # Extract surrounding windows for each sub_matrix
        kernel_windows = np.concatenate([w["windows"] for w in sub_mat_results], axis=2)
        # Compute and plot pileup
        pileup_fname = ("pileup_of_{n}_{pattern}_kernel_{kernel}").format(
            pattern=kernel_config["name"], n=kernel_windows.shape[2], kernel=kernel_id
        )
        kernel_pileup = pileup_patterns(kernel_windows)
        pileup_plot(kernel_pileup, name=pileup_fname, output=output)

    # for _, sub in hic_genome.sub_mats.iterrows():
    #        all_pattern_coords.append(converted_coords)
    #        all_pattern_windows.append(chrom_windows)

    # If no pattern detected on any chromosome, exit gracefully
    if len(all_pattern_coords) == 0:
        sys.stderr.write("No pattern detected ! Exiting.\n")
        sys.exit(0)

    # Combine patterns of all kernel matrices into a single array
    all_pattern_coords = np.concatenate(all_pattern_coords, axis=0)
    #

    # Remove patterns with overlapping windows (smeared patterns)
    good_patterns = remove_smears(all_pattern_coords, win_size=8)
    all_pattern_coords = all_pattern_coords[good_patterns, :]

    ### 2: WRITE OUTPUT
    write_results(all_pattern_coords, kernel_config["name"], output)
    # base_names = pathlib.Path(map_path).name


def main():
    arguments = docopt.docopt(__doc__, version=__version__)
    detect = arguments["detect"]
    generate_config = arguments["generate-config"]
    if detect:
        cmd_detect(arguments)
    elif generate_config:
        cmd_generate_config(arguments)
    return 0


if __name__ == "__main__":
    main()