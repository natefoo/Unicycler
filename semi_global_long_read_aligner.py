#!/usr/bin/env python
'''
Semi-global long read aligner

This is a script to align error-prone long reads (e.g. PacBio or Nanopore) to one or more
references in a semi-global manner. It uses GraphMap to do the actual alignment and then filters
the resulting alignments and outputs summaries.

Semi-global alignment allows for unpenalised end gaps, but the alignment will continue until one of
the two sequences ends. This includes cases where the two sequences overlap and cases where one
sequence is contained within the other:

  AAAAA        AAAAAAAAAAA         AAAAAAAA     AAAAAAAA
  |||||          |||||||           |||||           |||||
BBBBBBBBB        BBBBBBB       BBBBBBBBB           BBBBBBBBB

This tool is intended for cases where the reads and reference are expected to match perfectly (or
at least as perfectly as error-prone long reads can match). An example of an appropriate case would
be if the reference sequences are assembled contigs of a bacterial strain and the long reads are
from the same strain.

Required inputs:
  1) FASTA file of one or more reference sequences
  2) FASTQ file of long reads

Required outputs:
  1) SAM file of alignments

Optional outputs:
  1) SAM file of raw (unfiltered) GraphMap alignments
  2) Table files of depth and errors per base of each reference

Author: Ryan Wick
email: rrwick@gmail.com
'''
from __future__ import print_function
from __future__ import division

import subprocess
import sys
import os
import re
import random
import argparse
import string
import math
import time
from ctypes import CDLL, cast, c_char_p, c_int, c_double, c_void_p
from multiprocessing.dummy import Pool as ThreadPool

'''
VERBOSITY controls how much the script prints to the screen.
0 = nothing is printed
1 = a relatively simple output is printed
2 = a more thorough output is printed, including details on each Seqan alignment
3 = even more output is printed, including stuff from the C++ code
4 = tons of stuff is printed, including all k-mer positions in each Seqan alignment
'''
VERBOSITY = 0

'''
This script makes use of several C++ functions which are in seqan_align.so. They are wrapped in
similarly named Python functions.
'''
C_LIB = CDLL(os.path.join(os.path.dirname(os.path.realpath(__file__)),
                          'seqan_align/seqan_align.so'))


def main():
    '''
    If this script is run on its own, execution starts here.
    '''
    args = get_arguments()
    check_file_exists(args.ref)
    check_file_exists(args.reads)
    temp_dir_exist_at_start = os.path.exists(args.temp_dir)
    if not temp_dir_exist_at_start:
        os.makedirs(args.temp_dir)

    if VERBOSITY > 0:
        print()

    references = load_references(args.ref)
    read_dict, read_names = load_long_reads(args.reads)

    reads = semi_global_align_long_reads(references, args.ref, read_dict, read_names, args.reads, args.sam,
                                         args.temp_dir, args.path, args.threads, args.partial_ref,
                                         AlignmentScoringScheme(args.scores))
    summarise_errors(references, reads, args.table, args.keep)

    if not temp_dir_exist_at_start:
        os.rmdir(args.temp_dir)
    sys.exit(0)

def get_arguments():
    '''
    Specifies the command line arguments required by the script.
    '''
    parser = argparse.ArgumentParser(description='Semi-global long read aligner',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument('--ref', type=str, required=True, default=argparse.SUPPRESS,
                        help='FASTA file containing one or more reference sequences')
    parser.add_argument('--partial_ref', action='store_true',
                        help='Set if some reads are not expected to align to the reference.')
    parser.add_argument('--reads', type=str, required=True, default=argparse.SUPPRESS,
                        help='FASTQ file of long reads')
    parser.add_argument('--sam', type=str, required=True, default=argparse.SUPPRESS,
                        help='SAM file of resulting alignments')
    parser.add_argument('--table', type=str, required=False,
                        help='Path and/or prefix for table files summarising reference errors')
    parser.add_argument('--temp_dir', type=str, required=False, default='align_temp_PID',
                        help='Temp directory for working files')
    parser.add_argument('--path', type=str, required=False, default='graphmap',
                        help='Path to the GraphMap executable')
    parser.add_argument('--scores', type=str, required=False, default='3,-6,-5,-2',
                        help='Alignment scores: match, mismatch, gap open, gap extend')
    parser.add_argument('--threads', type=int, required=False, default=8,
                        help='Number of threads used by GraphMap')
    parser.add_argument('--verbosity', type=int, required=False, default=1,
                        help='Level of stdout information (0 to 4)')
    parser.add_argument('--keep', type=float, required=False, default=75.0,
                        help='Percentage of alignments to keep')

    args = parser.parse_args()

    global VERBOSITY
    VERBOSITY = args.verbosity

    # Add the process ID to the temp directory so multiple instances can run at once in the same
    # directory.
    if args.temp_dir == 'align_temp_PID':
        args.temp_dir = 'align_temp_' + str(os.getpid())

    return args

def semi_global_align_long_reads(references, ref_fasta, read_dict, read_names, reads_fastq, output_sam,
                                 temp_dir, graphmap_path, threads, partial_ref, scoring_scheme):
    '''
    This function does the primary work of this module: aligning long reads to references in an
    end-gap-free, semi-global manner. It returns a list of Read objects which contain their
    alignments.
    If seqan_all is True, then every Alignment object will be refined by using Seqan.
    If seqan_all is False, then only the overlap alignments and a small set of long contained
    alignments will be run through Seqan.
    '''
    # Run GraphMap and load in the resulting SAM file.
    graphmap_sam = os.path.join(temp_dir, 'graphmap_alignments.sam')
    run_graphmap(ref_fasta, reads_fastq, graphmap_sam, graphmap_path, threads, scoring_scheme)
    graphmap_alignments = load_sam_alignments(graphmap_sam, read_dict, references, scoring_scheme)
    if VERBOSITY > 2 and graphmap_alignments:
        print('All GraphMap alignments')
        print('-----------------------')
        for alignment in graphmap_alignments:
            print(alignment)
            if VERBOSITY > 3:
                print(alignment.cigar)
        print()

    # Use Seqan to extend the GraphMap alignments so they are fully semi-global. In this process,
    # some alignments will be discarded (those too far from being semi-global).
    semi_global_graphmap_alignments = extend_to_semi_global(graphmap_alignments, scoring_scheme)
    if VERBOSITY > 2 and semi_global_graphmap_alignments:
        print('GraphMap alignments after extension')
        print('-----------------------------------')
        for alignment in semi_global_graphmap_alignments:
            print(alignment)
            if VERBOSITY > 3:
                print(alignment.cigar)
        print()

    # Gather some statistics about the alignments.
    read_to_refs = [x.get_read_to_ref_ratio() for x in graphmap_alignments]
    percent_ids = [x.percent_identity for x in graphmap_alignments]
    scores = [x.scaled_score for x in graphmap_alignments]
    read_to_ref_median, read_to_ref_mad = get_median_and_mad(read_to_refs)
    percent_id_median, percent_id_mad = get_median_and_mad(percent_ids)
    score_median, score_mad = get_median_and_mad(scores)
    if VERBOSITY > 0:
        print_alignment_summary_table(graphmap_alignments, read_to_ref_median, read_to_ref_mad,
                                      percent_id_median, percent_id_mad, score_median, score_mad)

    # Give the alignments to their corresponding reads.
    for alignment in semi_global_graphmap_alignments:
        read_dict[alignment.read.name].alignments.append(alignment)

    # We can now sort our reads into two different categories for further action:
    #   1) Reads with a single, high quality alignment in the middle of a reference. These reads
    #      are done!
    #   2) Reads that are incompletely (or not at all) aligned, have overlapping alignments or
    #      low quality alignments. These reads will be aligned again using Seqan.
    low_score_threshold = score_median - (3 * score_mad)
    completed_reads = []
    reads_to_realign = []
    for read_name in read_names:
        read = read_dict[read_name]
        if read.needs_realignment(partial_ref, low_score_threshold):
            reads_to_realign.append(read)
        else:
            completed_reads.append(read)


    # OPTIONAL TO DO: for reads which are completed, I could still try to refine them using my Seqan code.
    #                 I'm not sure if it's worth it, so I should give it a try to see what kind of difference it makes.


    # Realign unfinished reads using Seqan.
    if VERBOSITY > 0:
        num_realignments = len(reads_to_realign)
        max_v = len(read_dict)
        print('Realigning reads')
        print('----------------')
        print('Completed reads:      ', int_to_str(len(completed_reads), max_v))
        print('Reads to be realigned:', int_to_str(num_realignments, max_v))
        print()
    if VERBOSITY == 1:
        print_progress_line(0, num_realignments)
    if read_to_ref_median:
        expected_ref_to_read_ratio = 1.0 / read_to_ref_median
    else:
        expected_ref_to_read_ratio = 0.95 # TO DO: SET THIS TO AN EMPIRICALLY-DERIVED VALUE
    completed_count = 0

    # Create a C++ KmerPositions object and add each reference sequence.
    kmer_positions_ptr = C_LIB.newKmerPositions()
    for ref in references:
        C_LIB.addKmerPositions(kmer_positions_ptr, ref.name, ref.sequence)

    # If single-threaded, just do the work in a simple loop.
    if threads == 1:
        for read in reads_to_realign:
            new_alignments, output = seqan_alignment_one_read_all_refs(read, references,
                                                                       scoring_scheme,
                                                                       expected_ref_to_read_ratio,
                                                                       kmer_positions_ptr)
            read.alignments += new_alignments
            completed_count += 1
            if VERBOSITY == 1:
                print_progress_line(completed_count, num_realignments)
            if VERBOSITY > 1:
                print(output, end='')

    # If multi-threaded, use a thread pool.
    else:
        pool = ThreadPool(threads)
        arg_list = []
        for read in reads_to_realign:
            arg_list.append((read, references, scoring_scheme, expected_ref_to_read_ratio, kmer_positions_ptr))
        for new_alignments, output in pool.imap_unordered(seqan_alignment_one_read_all_refs_one_arg,
                                                          arg_list, 1):
            if new_alignments:
                read = new_alignments[0].read
                read.alignments += new_alignments
            completed_count += 1
            if VERBOSITY == 1:
                print_progress_line(completed_count, num_realignments)
            if VERBOSITY > 1:
                print(output, end='')

    # We're done with the C++ KmerPositions object, so delete it now.
    C_LIB.deleteAllKmerPositions(kmer_positions_ptr)

    # Filter the alignments based on conflicting read position.
    if VERBOSITY > 0:
        all_alignments_count = sum([len(x.alignments) for x in read_dict.itervalues()])
        print('Removing conflicting alignments')
        print('-------------------------------')
        print('Alignments before filtering:        ', int_to_str(all_alignments_count, max_v))
    filtered_alignments = []
    for read in read_dict.itervalues():
        read.remove_conflicting_alignments()
        filtered_alignments += read.alignments
    if VERBOSITY > 0:
        print('Alignments after conflict filtering:', int_to_str(len(filtered_alignments),
                                                                 max_v))
        print()


    # OPTIONAL TO DO: filter the alignments based on ID
    #     for read in read_dict.itervalues():
    #         read.remove_low_id_alignments(low_id_cutoff)


    # Output a summary of the reads' alignments.
    fully_aligned, partially_aligned, unaligned = group_reads_by_fraction_aligned(read_dict)
    if VERBOSITY > 0:
        print('Read summary')
        print('------------')
        max_v = len(read_dict)
        print('Total read count:       ', int_to_str(len(read_dict), max_v))
        print('Fully aligned reads:    ', int_to_str(len(fully_aligned), max_v))
        print('Partially aligned reads:', int_to_str(len(partially_aligned), max_v))
        print('Unaligned reads:        ', int_to_str(len(unaligned), max_v))
        print()

    # Save the final alignments to SAM file.
    final_alignments = []
    for read_name in read_names:
        read = read_dict[read_name]
        final_alignments += read.alignments
    write_sam_file(final_alignments, graphmap_sam, output_sam)
    
    os.remove(graphmap_sam)



    # # TEMP - print out information about reads with multiple alignments and save them to file.
    # overlapping_reads_fasta = open('/Users/Ryan/Desktop/synthetic_aligning/overlapping_reads.fasta', 'w')
    # overlapping_reads_fastq = open('/Users/Ryan/Desktop/synthetic_aligning/overlapping_reads.fastq', 'w')
    # print('Multi-alignment reads')
    # print('---------------------')
    # for read in read_dict.itervalues():
    #     if len(read.alignments) > 1:
    #         print(read.get_descriptive_string())
    #         overlapping_reads_fasta.write(read.get_fasta())
    #         overlapping_reads_fastq.write(read.get_fastq())
    # print('Incompletely aligned reads')
    # print('--------------------------')
    # for read in partially_aligned:
    #     print(read.get_descriptive_string())
    # quit()



    return read_dict

def summarise_errors(references, long_reads, table_prefix, percent_alignments_to_keep):
    '''
    Writes a table file summarising the alignment errors in terms of reference sequence position.
    Works in a brute force manner - could be made more efficient later if necessary.
    '''
    # If we are not making table files or printing a summary, then quit now because there's nothing
    # else to do.
    if not table_prefix and VERBOSITY == 0:
        return

    # We are be willing to throw out the worst alignments for any particular location. This value
    # specifies how many of our alignments we'll keep.
    # E.g. if 0.75, we'll throw out up to 25% of the alignments for each reference location.
    frac_alignments_to_keep = percent_alignments_to_keep / 100.0

    # Ensure the table prefix is nicely formatted and any necessary directories are made.
    if table_prefix:
        if os.path.isdir(table_prefix) and not table_prefix.endswith('/'):
            table_prefix += '/'
        if table_prefix.endswith('/') and not os.path.isdir(table_prefix):
            os.makedirs(table_prefix)
        if not table_prefix.endswith('/'):
            directory = os.path.dirname(table_prefix)
            if directory and not os.path.isdir(directory):
                os.makedirs(directory)

    if VERBOSITY > 0:
        # So the numbers align nicely, we look for and remember the largest number to be displayed.
        max_v = max(100,
                    sum([len(x.alignments) for x in long_reads.itervalues()]),
                    max([len(x.sequence) for x in references]))
        print('Alignment summaries per reference')
        print('---------------------------------')

    for reference in references:
        # Create a table file for the reference.
        if table_prefix:
            header_for_filename = clean_str_for_filename(reference.name)
            if table_prefix.endswith('/'):
                table_filename = table_prefix + header_for_filename + '.txt'
            else:
                table_filename = table_prefix + '_' + header_for_filename + '.txt'
            table = open(table_filename, 'w')
            table.write('\t'.join(['base',
                                   'total read depth',
                                   'filtered depth',
                                   'mismatches',
                                   'deletions',
                                   'insertions',
                                   'mismatch rate',
                                   'deletion rate',
                                   'insertion rate',
                                   'insertion sizes']) + '\n')

        # Gather up the alignments for this reference and count up the depth for each reference
        # position.
        seq_len = len(reference.sequence)
        total_depths = [0] * seq_len
        alignments = []
        for read in long_reads.itervalues():
            for alignment in read.alignments:
                if alignment.ref.name == reference.name:
                    alignments.append(alignment)
                    for pos in xrange(alignment.ref_start_pos, alignment.ref_end_pos):
                        total_depths[pos] += 1

        # Discard as many of the worst alignments as we can while keeping a sufficient read depth
        # at each position.
        sufficient_depths = [math.ceil(frac_alignments_to_keep * x) for x in total_depths]
        filtered_depths = total_depths[:]
        filtered_alignments = []
        alignments = sorted(alignments, key=lambda x: x.scaled_score)
        for alignment in alignments:
            removal_okay = True
            for pos in xrange(alignment.ref_start_pos, alignment.ref_end_pos):
                if filtered_depths[pos] - 1 < sufficient_depths[pos]:
                    removal_okay = False
                    break
            if removal_okay:
                for pos in xrange(alignment.ref_start_pos, alignment.ref_end_pos):
                    filtered_depths[pos] -= 1
            else:
                filtered_alignments.append(alignment)

        # Determine how much of the reference sequence has at least one aligned read.
        aligned_len = sum([1 for x in filtered_depths if x > 0])
        aligned_percent = 100.0 * aligned_len / seq_len

        # Using the filtered alignments, add up mismatches, deletions and insertions and also get
        # their rates (divided by the depth).
        # Insertions are counted in two ways:
        #   1) With multi-base insertions totalled up. E.g. 3I results in 3 counts all in the same
        #      reference location.
        #   2) With multi-base insertions counted only once. E.g. 3I results in 1 count at the
        #      reference location.
        mismatches = [0] * seq_len
        deletions = [0] * seq_len
        insertions = [0] * seq_len
        insertion_sizes = {}

        for alignment in filtered_alignments:
            for pos in alignment.ref_mismatch_positions:
                mismatches[pos] += 1
            for pos in alignment.ref_deletion_positions:
                deletions[pos] += 1
            for pos, size in alignment.ref_insertion_positions_and_sizes:
                insertions[pos] += 1
                if pos not in insertion_sizes:
                    insertion_sizes[pos] = []
                insertion_sizes[pos].append(size)
        mismatch_rates = []
        deletion_rates = []
        insertion_rates = []
        insertion_rates_multi_base = []
        for i in xrange(seq_len):
            mismatch_rate = 0.0
            deletion_rate = 0.0
            insertion_rate = 0.0
            insertion_rate_multi_base = 0.0
            if filtered_depths[i] > 0:
                mismatch_rate = mismatches[i] / filtered_depths[i]
                deletion_rate = deletions[i] / filtered_depths[i]
                insertion_rate = insertions[i] / filtered_depths[i]
                if i in insertion_sizes:
                    insertion_rate_multi_base = sum(insertion_sizes[i]) / filtered_depths[i]
            mismatch_rates.append(mismatch_rate)
            deletion_rates.append(deletion_rate)
            insertion_rates.append(insertion_rate)
            insertion_rates_multi_base.append(insertion_rate_multi_base)

        if table_prefix:
            for i in xrange(seq_len):
                insertion_sizes_str = ''
                if i in insertion_sizes:
                    insertion_sizes_str = ', '.join([str(x) for x in insertion_sizes[i]])
                table.write('\t'.join([str(i+1),
                                       str(total_depths[i]),
                                       str(filtered_depths[i]),
                                       str(mismatches[i]),
                                       str(deletions[i]),
                                       str(insertions[i]),
                                       str(mismatch_rates[i]),
                                       str(deletion_rates[i]),
                                       str(insertion_rates[i]),
                                       insertion_sizes_str]))
                table.write('\n')
            table.close()

        if VERBOSITY > 0:
            contained_alignment_count = 0
            overlapping_alignment_count = 0
            for alignment in filtered_alignments:
                if alignment.is_whole_read():
                    contained_alignment_count += 1
                else:
                    overlapping_alignment_count += 1
            print(reference.name)
            print('  Total length:       ', int_to_str(seq_len, max_v) + ' bp')
            if alignments:
                mean_depth = sum(filtered_depths) / seq_len
                mean_mismatch_rate = 100.0 * sum(mismatch_rates) / aligned_len
                mean_insertion_rate = 100.0 * sum(insertion_rates_multi_base) / aligned_len
                mean_deletion_rate = 100.0 * sum(deletion_rates) / aligned_len
                if VERBOSITY == 1:
                    print('  Alignments:         ', int_to_str(len(filtered_alignments), max_v))
                if VERBOSITY > 1:
                    print('  Total alignments:   ', int_to_str(len(alignments), max_v))
                    print('  Filtered alignments:', int_to_str(len(filtered_alignments), max_v))
                    print('    Contained:        ', int_to_str(contained_alignment_count, max_v))
                    print('    Overlapping:      ', int_to_str(overlapping_alignment_count, max_v))
                print('  Covered length:     ', int_to_str(aligned_len, max_v) + ' bp')
                print('  Covered fraction:   ', float_to_str(aligned_percent, 2, max_v) + '%')
                if VERBOSITY > 1:
                    print('  Mean read depth:    ', float_to_str(mean_depth, 2, max_v))
                    print('  Mismatch rate:      ',
                          float_to_str(mean_mismatch_rate, 2, max_v) +'%')
                    print('  Insertion rate:     ',
                          float_to_str(mean_insertion_rate, 2, max_v) + '%')
                    print('  Deletion rate:      ',
                          float_to_str(mean_deletion_rate, 2, max_v) + '%')
            else:
                print('  Total alignments:   ', int_to_str(0, max_v))
            print()

def print_alignment_summary_table(graphmap_alignments, read_to_ref_median, read_to_ref_mad,
                                  percent_id_median, percent_id_mad, score_median, score_mad):
    '''
    Prints a small table showing some details about the GraphMap alignments.
    '''
    print('Alignment summary')
    print('-----------------')
    print('Total GraphMap alignments:', int_to_str(len(graphmap_alignments)))
    print()

    table_lines = ['',
                   'Read / reference length:',
                   'Percent identity:',
                   'Score:']

    pad_length = max([len(x) for x in table_lines]) + 2
    table_lines = [x.ljust(pad_length) for x in table_lines]

    table_lines[0] += 'Median'
    table_lines[1] += float_to_str(read_to_ref_median, 3)
    table_lines[2] += float_to_str(percent_id_median, 2) + '%'
    table_lines[3] += float_to_str(score_median, 2)

    pad_length = max([len(x) for x in table_lines]) + 2
    table_lines = [x.ljust(pad_length) for x in table_lines]

    table_lines[0] += 'MAD'
    table_lines[1] += float_to_str(read_to_ref_mad, 3)
    table_lines[2] += float_to_str(percent_id_mad, 2) + '%'
    table_lines[3] += float_to_str(score_mad, 2)

    for line in table_lines:
        print(line)
    print()

def extend_to_semi_global(alignments, scoring_scheme):
    '''
    This function returns truly semi-global alignments made from the input alignments.
    '''
    if VERBOSITY > 3 and alignments:
        print('Extending aligments')
        print('-------------------')

    allowed_missing_bases = 100 # TO DO: MAKE THIS A PARAMETER
    semi_global_alignments = []
    for alignment in alignments:
        total_missing_bases = alignment.get_total_missing_bases()

        # If an input alignment is already semi-global, then it's included in the output.
        if total_missing_bases == 0:
            semi_global_alignments.append(alignment)

        # If an input alignment is almost semi-global (below a threshold), and not too close to the
        # end of the reference, then it is extended to make it semi-global.
        elif total_missing_bases <= allowed_missing_bases:
            missing_start = alignment.get_missing_bases_at_start()
            missing_end = alignment.get_missing_bases_at_end()
            if missing_start and alignment.ref_start_pos >= 2 * missing_start:
                alignment.extend_start(scoring_scheme)
            if missing_end and alignment.ref_end_gap >= 2 * missing_end:
                alignment.extend_end(scoring_scheme)
            semi_global_alignments.append(alignment)

        # If an input alignment is above the threshold (not close to being semi-global), it is
        # discarded.
        else:
            pass

    return semi_global_alignments

def load_references(fasta_filename):
    '''
    This function loads in sequences from a FASTA file and returns a list of Reference objects.
    '''
    references = []
    total_bases = 0
    if VERBOSITY > 0:
        print('Loading references')
        print('------------------')
        num_refs = sum(1 for line in open(fasta_filename) if line.startswith('>'))
        if not num_refs:
            quit_with_error('There are no references sequences in ' + fasta_filename)
        print_progress_line(0, num_refs)
    fasta_file = open(fasta_filename, 'r')
    name = ''
    sequence = ''
    last_progress = 0.0
    for line in fasta_file:
        line = line.strip()
        if not line:
            continue
        if line.startswith('>'): # Header line = start of new contig
            if name:
                references.append(Reference(name, sequence))
                total_bases += len(sequence)
                if VERBOSITY > 0:
                    progress = 100.0 * len(references) / num_refs
                    progress_rounded_down = float(int(progress))
                    if progress_rounded_down > last_progress:
                        print_progress_line(len(references), num_refs, total_bases)
                        last_progress = progress_rounded_down    
                name = ''
                sequence = ''
            name = get_nice_header(line[1:])
        else:
            sequence += line
    fasta_file.close()
    if name:
        references.append(Reference(name, sequence))
        total_bases += len(sequence)
        if VERBOSITY > 0:
            print_progress_line(len(references), num_refs, total_bases)
            print('\n')
    return references


def load_long_reads(fastq_filename):
    '''
    This function loads in long reads from a FASTQ file and returns a dictionary where key = read
    name and value = Read object. It also returns a list of read names, in the order they are in
    the file.
    '''
    read_dict = {}
    read_names = []
    total_bases = 0
    last_progress = 0.0
    if VERBOSITY > 0:
        print('Loading reads')
        print('-------------')
        num_reads = sum(1 for line in open(fastq_filename)) // 4
        print_progress_line(0, num_reads)
    fastq = open(fastq_filename, 'r')
    for line in fastq:
        name = line.strip()[1:]
        sequence = next(fastq).strip()
        _ = next(fastq)
        qualities = next(fastq).strip()
        read_dict[name] = Read(name, sequence, qualities)
        read_names.append(name)
        total_bases += len(sequence)
        if VERBOSITY > 0:
            progress = 100.0 * len(read_dict) / num_reads
            progress_rounded_down = float(int(progress))
            if progress_rounded_down > last_progress:
                print_progress_line(len(read_dict), num_reads, total_bases)
                last_progress = progress_rounded_down    
    fastq.close()
    if VERBOSITY > 0:
        print('\n')
    return read_dict, read_names

def run_graphmap(fasta, long_reads_fastq, sam_file, graphmap_path, threads, scoring_scheme):
    '''
    This function runs GraphMap for the given inputs and produces a SAM file at the given location.
    '''
    graphmap_version = get_graphmap_version(graphmap_path)

    # Build the GraphMap command. There is a bit of difference if we're using a version before or
    # after v0.3.0.
    command = [graphmap_path]
    if graphmap_version >= 0.3:
        command.append('align')
    command += ['-r', fasta,
                '-d', long_reads_fastq,
                '-o', sam_file,
                '-t', str(threads),
                '-a', 'anchorgotoh']
    command += scoring_scheme.get_graphmap_parameters()

    if VERBOSITY > 0:
        print('Running GraphMap')
        print('----------------')
        print(' '.join(command))
        print()

    # Print the GraphMap output as it comes. I gather up and display lines so I can display fewer
    # progress lines. This helps when piping the output to file (otherwise the output can be
    # excessively large).
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    line = ''
    last_progress = -1.0
    while process.poll() is None:
        graphmap_output = process.stderr.read(1)
        if VERBOSITY > 0:
            line += graphmap_output
            if line.endswith('\r'):
                line = line.strip() + '\r'
            if line.endswith('\n') or line.endswith('\r'):
                if line.strip():
                    if 'CPU time' in line:
                        progress = float(line.split('(')[1].split(')')[0][:-1])
                        progress_rounded_down = float(int(progress))
                        if progress_rounded_down > last_progress:
                            print(line, end='')
                            last_progress = progress_rounded_down
                    else:
                        print(line, end='')
                line = ''
    if VERBOSITY > 0:
        print()

    # Clean up.
    if os.path.isfile(fasta + '.gmidx'):
        os.remove(fasta + '.gmidx')
    if os.path.isfile(fasta + '.gmidxsec'):
        os.remove(fasta + '.gmidxsec')

    if not os.path.isfile(sam_file):
        quit_with_error('GraphMap failure')

def get_graphmap_version(graphmap_path):
    '''
    Returns the version of GraphMap.
    '''
    command = [graphmap_path, '-h']
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = process.communicate()
    allout = out + err
    if 'Version: v' not in allout:
        command = [graphmap_path, 'align', '-h']
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = process.communicate()
        allout = out + err
    if 'Version: v' not in allout:
        return 0.0
    version_i = allout.find('Version: v')
    version = allout[version_i + 10:]
    version = version.split()[0]
    version = '.'.join(version.split('.')[0:2])
    return float(version)

def load_sam_alignments(sam_filename, read_dict, references, scoring_scheme):
    '''
    This function returns a list of Alignment objects from the given SAM file.
    '''
    reference_dict = {x.name: x for x in references}
    sam_alignments = []
    sam_file = open(sam_filename, 'r')
    for line in sam_file:
        line = line.strip()
        if line and not line.startswith('@') and line.split('\t', 3)[2] != '*':
            sam_alignments.append(Alignment(sam_line=line, read_dict=read_dict,
                                            reference_dict=reference_dict,
                                            scoring_scheme=scoring_scheme))
    return sam_alignments

def seqan_alignment_one_read_all_refs_one_arg(all_args):
    '''
    This is just a one-argument version of seqan_alignment_one_read_all_refs to make it easier to
    use that function in a thread pool.
    '''
    read, references, scoring_scheme, expected_ref_to_read_ratio, kmer_positions_ptr = all_args
    return seqan_alignment_one_read_all_refs(read, references, scoring_scheme,
                                             expected_ref_to_read_ratio, kmer_positions_ptr)

def seqan_alignment_one_read_all_refs(read, references, scoring_scheme,
                                      expected_ref_to_read_ratio, kmer_positions_ptr):
    '''
    Aligns a single read against all reference sequences using Seqan. Both forward and reverse
    complement alignments are tried. Returns a list of all alignments found and the console output.
    '''
    start_time = time.time()
    output = ''
    if VERBOSITY > 1:
        output += str(read) + '\n'
    alignments = []

    add_read_kmer_positions(kmer_positions_ptr, read)

    for ref in references:
        if VERBOSITY > 3:
            output += 'Reference: ' + ref.name + '+\n'
        forward_alignments, forward_alignment_output = \
                        seqan_alignment_one_read_one_ref(read, ref, False, scoring_scheme,
                                                         expected_ref_to_read_ratio,
                                                         kmer_positions_ptr)
        alignments += forward_alignments
        output += forward_alignment_output

        if VERBOSITY > 3:
            output += 'Reference: ' + ref.name + '-\n'
        reverse_alignments, reverse_alignment_output = \
                        seqan_alignment_one_read_one_ref(read, ref, True, scoring_scheme,
                                                         expected_ref_to_read_ratio,
                                                         kmer_positions_ptr)
        alignments += reverse_alignments
        output += reverse_alignment_output

    if VERBOSITY > 3:
        output += 'Final alignments for ' + read.name + ':\n'
    if VERBOSITY > 1:
        if not alignments:
            output += 'No alignments found for read ' + read.name + '\n'
        else:
            for alignment in alignments:
                output += '  ' + str(alignment) + '\n'
                if VERBOSITY > 3:
                    output += alignment.cigar + '\n'
        if VERBOSITY > 2:
            output += '  Time to align: ' + float_to_str(time.time() - start_time, 3) + ' s\n'
        output += '\n'

    delete_read_kmer_positions(kmer_positions_ptr, read)

    return alignments, output

def seqan_alignment_one_read_one_ref(read, ref, rev_comp, scoring_scheme, expected_slope,
                                     kmer_positions_ptr):
    '''
    Runs an alignment using Seqan between one read and one reference.
    Returns a list of Alignment objects: empty list means it did not succeed, a list of one means
    it got one alignment and a list of more than one means it got multiple alignments.
    '''
    if rev_comp:
        read_name = read.name + '-'
        read_seq = reverse_complement(read.sequence)
    else:
        read_name = read.name + '+'
        read_seq = read.sequence

    # print('READ:', read_name, 'REF:', ref.name) # TEST CODE - USEFUL FOR DEBUGGING

    ptr = C_LIB.semiGlobalAlignment(read_name, read_seq, ref.name, ref.sequence,
                                    expected_slope, VERBOSITY, kmer_positions_ptr,
                                    scoring_scheme.match, scoring_scheme.mismatch,
                                    scoring_scheme.gap_open, scoring_scheme.gap_extend)
    results = c_string_to_python_string(ptr).split(';')
    alignment_strings = results[:-1]
    output = results[-1]

    alignments = []
    for alignment_string in alignment_strings:
        alignment = Alignment(seqan_output=alignment_string, read=read, ref=ref, rev_comp=rev_comp,
                              scoring_scheme=scoring_scheme)
        alignments.append(alignment)

    return alignments, output

def get_ref_shift_from_cigar_part(cigar_part):
    '''
    This function returns how much a given cigar moves on a reference.
    Examples:
      * '5M' returns 5
      * '5S' returns 0
      * '5D' returns 5
      * '5I' returns 0
    '''
    if cigar_part[-1] == 'M':
        return int(cigar_part[:-1])
    if cigar_part[-1] == 'D':
        return int(cigar_part[:-1])
    if cigar_part[-1] == 'S':
        return 0
    if cigar_part[-1] == 'I':
        return 0

def simplify_ranges(ranges):
    '''
    Collapses overlapping ranges together. Input ranges are tuples of (start, end) in the normal
    Python manner where the end isn't included.
    '''
    fixed_ranges = []
    for int_range in ranges:
        if int_range[0] > int_range[1]:
            fixed_ranges.append((int_range[1], int_range[0]))
        elif int_range[0] < int_range[1]:
            fixed_ranges.append(int_range)
    starts_ends = [(x[0], 1) for x in fixed_ranges]
    starts_ends += [(x[1], -1) for x in fixed_ranges]
    starts_ends.sort(key=lambda x: x[0])
    current_sum = 0
    cumulative_sum = []
    for start_end in starts_ends:
        current_sum += start_end[1]
        cumulative_sum.append((start_end[0], current_sum))
    prev_depth = 0
    start = 0
    combined = []
    for pos, depth in cumulative_sum:
        if prev_depth == 0:
            start = pos
        elif depth == 0:
            combined.append((start, pos))
        prev_depth = depth
    return combined

def range_is_contained(test_range, other_ranges):
    '''
    Returns True if test_range is entirely contained within any range in other_ranges.
    '''
    start, end = test_range
    for other_range in other_ranges:
        if other_range[0] <= start and other_range[1] >= end:
            return True
    return False

def write_sam_file(alignments, graphmap_sam, sam_filename):
    '''
    Writes the given alignments to a SAM file.
    '''
    sam_file = open(sam_filename, 'w')

    # Copy the header from the GraphMap SAM file.
    graphmap_sam_file = open(graphmap_sam, 'r')
    for line in graphmap_sam_file:
        if line.startswith('@'):
            sam_file.write(line)
        else:
            break

    for alignment in alignments:
        sam_file.write(alignment.get_sam_line())
        sam_file.write('\n')
    sam_file.close()

def is_header_spades_format(contig_name):
    '''
    Returns whether or not the header appears to be in the SPAdes/Velvet format.
    Example: NODE_5_length_150905_cov_4.42519
    '''
    contig_name_parts = contig_name.split('_')
    return len(contig_name_parts) > 5 and \
           (contig_name_parts[0] == 'NODE' or contig_name_parts[0] == 'EDGE') and \
           contig_name_parts[2] == 'length' and contig_name_parts[4] == 'cov'

def get_nice_header(header):
    '''
    For a header with a SPAdes/Velvet format, this function returns a simplified string that is
    just NODE_XX where XX is the contig number.
    For any other format, this function trims off everything following the first whitespace.
    '''
    if is_header_spades_format(header):
        return 'NODE_' + header.split('_')[1]
    else:
        return header.split()[0]

def check_file_exists(filename): # type: (str) -> bool
    '''
    Checks to make sure the single given file exists.
    '''
    if not os.path.isfile(filename):
        quit_with_error('could not find ' + filename)

def quit_with_error(message): # type: (str) -> None
    '''
    Displays the given message and ends the program's execution.
    '''
    print('Error:', message, file=sys.stderr)
    sys.exit(1)

def float_to_str(num, decimals, max_num=0):
    '''
    Converts a number to a string. Will add left padding based on the max value to ensure numbers
    align well.
    '''
    num_str = '%.' + str(decimals) + 'f'
    num_str = num_str % num
    after_decimal = num_str.split('.')[1]
    num_str = int_to_str(int(num)) + '.' + after_decimal
    if max_num > 0:
        max_str = float_to_str(max_num, decimals)
        num_str = num_str.rjust(len(max_str))
    return num_str

def int_to_str(num, max_num=0):
    '''
    Converts a number to a string. Will add left padding based on the max value to ensure numbers
    align well.
    '''
    num_str = '{:,}'.format(num)
    max_str = '{:,}'.format(int(max_num))
    return num_str.rjust(len(max_str))

# def line_count(filename):
#     '''
#     Counts the lines in the given file.
#     '''
#     i = 0
#     with open(filename) as file_to_count:
#         for i, _ in enumerate(file_to_count):
#             pass
#     return i + 1

def clean_str_for_filename(filename):
    '''
    This function removes characters from a string which would not be suitable in a filename.
    It also turns spaces into underscores, because filenames with spaces can occasionally cause
    issues.
    http://stackoverflow.com/questions/295135/turn-a-string-into-a-valid-filename-in-python
    '''
    valid_chars = "-_.() %s%s" % (string.ascii_letters, string.digits)
    filename_valid_chars = ''.join(c for c in filename if c in valid_chars)
    return filename_valid_chars.replace(' ', '_')

def reverse_complement(seq):
    '''Given a DNA sequences, this function returns the reverse complement sequence.'''
    rev_comp = ''
    for i in reversed(range(len(seq))):
        rev_comp += complement_base(seq[i])
    return rev_comp

def complement_base(base):
    '''Given a DNA base, this returns the complement.'''
    forward = 'ATGCatgcRYSWKMryswkmBDHVbdhvNn.-?'
    reverse = 'TACGtacgYRSWMKyrswmkVHDBvhdbNn.-?N'
    return reverse[forward.find(base)]

def get_median(num_list):
    '''
    Returns the median of the given list of numbers.
    '''
    count = len(num_list)
    if count == 0:
        return 0.0
    sorted_list = sorted(num_list)
    if count % 2 == 0:
        return (sorted_list[count // 2 - 1] + sorted_list[count // 2]) / 2.0
    else:
        return sorted_list[count // 2]

def get_median_and_mad(num_list):
    '''
    Returns the median and MAD of the given list of numbers.
    '''
    median = get_median(num_list)
    absolute_deviations = [abs(x - median) for x in num_list]
    mad = 1.4826 * get_median(absolute_deviations)
    return median, mad


def get_mean_and_st_dev(num_list):
    '''
    This function returns the mean and standard deviation of the given list of numbers.
    '''
    num = len(num_list)
    if num == 0:
        return 0.0, 0.0
    mean = sum(num_list) / num
    if num == 1:
        return mean, -1
    sum_squares = sum((x - mean) ** 2 for x in num_list)
    st_dev = (sum_squares / (num - 1)) ** 0.5
    return mean, st_dev

def print_progress_line(completed, total, base_pairs=None):
    '''
    Prints a progress line to the screen using a carriage return to overwrite the previous progress
    line.
    '''
    progress_str = int_to_str(completed) + ' / ' + int_to_str(total)
    progress_str += ' (' + '%.1f' % (100.0 * completed / total) + '%)'
    if base_pairs is not None:
        progress_str += ' - ' + int_to_str(base_pairs) + ' bp'
    print('\r' + progress_str, end='')
    sys.stdout.flush()

def group_reads_by_fraction_aligned(read_dict):
    '''
    Groups reads into three lists:
      1) Fully aligned
      2) Partially aligned
      3) Unaligned
    '''
    fully_aligned_reads = []
    partially_aligned_reads = []
    unaligned_reads = []
    for read in read_dict.itervalues():
        fraction_aligned = read.get_fraction_aligned()
        if fraction_aligned == 1.0:
            fully_aligned_reads.append(read)
        elif fraction_aligned == 0.0:
            unaligned_reads.append(read)
        else:
            partially_aligned_reads.append(read)
    return fully_aligned_reads, partially_aligned_reads, unaligned_reads




class AlignmentScoringScheme(object):
    '''
    This class holds an alignment scoring scheme.
    '''
    def __init__(self, scheme_string):
        scheme_parts = scheme_string.split(',')

        # Default scoring scheme
        self.match = 3
        self.mismatch = -6
        self.gap_open = -5
        self.gap_extend = -2

        if len(scheme_parts) == 4:
            self.match = int(scheme_parts[0])
            self.mismatch = int(scheme_parts[1])
            self.gap_open = int(scheme_parts[2])
            self.gap_extend = int(scheme_parts[3])

        assert self.match > 0
        assert self.mismatch < 0
        assert self.gap_open < 0
        assert self.gap_extend < 0

    def get_graphmap_parameters(self):
        '''
        Returns the scoring scheme in the form of GraphMap parameters for subprocess.
        '''
        return ['-M', str(self.match),
                '-X', str(-self.mismatch),
                '-G', str(-self.gap_open),
                '-E', str(-self.gap_extend)]



class Reference(object):
    '''
    This class holds a reference sequence: just a name and a nucleotide sequence.
    '''
    def __init__(self, name, sequence):
        self.name = name
        self.sequence = sequence

    def get_length(self):
        '''
        Returns the sequence length.
        '''
        return len(self.sequence)



class Read(object):
    '''
    This class holds a long read, e.g. from PacBio or Oxford Nanopore.
    '''
    def __init__(self, name, sequence, qualities):
        self.name = name
        self.sequence = sequence
        self.qualities = qualities
        self.alignments = []

    def __repr__(self):
        return self.name + ' (' + str(len(self.sequence)) + ' bp)'

    def get_length(self):
        '''
        Returns the sequence length.
        '''
        return len(self.sequence)

    def needs_realignment(self, partial_ref, low_score_threshold):
        '''
        This function returns True or False based on whether a read was nicely aligned by GraphMap
        or needs to be realigned with Seqan.
        '''
        # If we know the reference to be partial, then unaligned reads are expected. So in the case
        # of a partial reference, we will not realign an unaligned read.
        if partial_ref and not self.alignments:
            return False

        # Either zero or more than one alignments result in realignment.
        if len(self.alignments) != 1:
            return True

        # Overlapping alignments or low quality alignments result in realignment.
        only_alignment = self.alignments[0]
        return (not only_alignment.is_whole_read() or
                only_alignment.scaled_score < low_score_threshold)

    def remove_conflicting_alignments(self):
        '''
        This function removes alignments from the read which are likely to be spurious. It sorts
        alignments by score and works through them from highest score to lowest score, only keeping
        alignments that cover new parts of the read.
        '''
        self.alignments = sorted(self.alignments, reverse=True,
                                 key=lambda x: (x.scaled_score, random.random()))
        kept_alignments = []
        read_ranges = []
        for alignment in self.alignments:
            read_range = alignment.read_start_end_positive_strand()
            if not range_is_contained(read_range, read_ranges):
                read_ranges.append(read_range)
                read_ranges = simplify_ranges(read_ranges)
                kept_alignments.append(alignment)
        self.alignments = kept_alignments

#     def remove_low_id_alignments(self, id_threshold):
#         '''
#         This function removes alignments with identity below the cutoff.
#         '''
#         self.alignments = [x for x in self.alignments if x.percent_identity >= id_threshold]

    def get_fastq(self):
        '''
        Returns a string for the read in FASTQ format. It contains four lines and ends in a line
        break.
        '''
        return '@' + self.name + '\n' + \
               self.sequence + '\n' + \
               '+' + self.name + '\n' + \
               self.qualities + '\n'

    def get_fasta(self):
        '''
        Returns a string for the read in FASTA format. It contains two lines and ends in a line
        break.
        '''
        return '>' + self.name + '\n' + \
               self.sequence + '\n'

    def get_descriptive_string(self):
        '''
        Returns a multi-line string that describes the read and its alignments.
        '''
        header = self.name + ' (' + str(len(self.sequence)) + ' bp)'
        line = '-' * len(header)
        description = header + '\n' + line + '\n'
        if not self.alignments:
            description += 'no alignments'
        else:
            description += '%.2f' % (100.0 * self.get_fraction_aligned()) + '% aligned\n'
            description += '\n'.join([str(x) for x in self.alignments])
        return description + '\n\n'

    def get_fraction_aligned(self):
        '''
        This function returns the fraction of the read which is covered by any of the read's
        alignments.
        '''
        read_ranges = [x.read_start_end_positive_strand() for x in self.alignments]
        read_ranges = simplify_ranges(read_ranges)
        aligned_length = sum([x[1] - x[0] for x in read_ranges])
        return aligned_length / len(self.sequence)



class Alignment(object):
    '''
    This class describes an alignment between a long read and a contig.
    It can be constructed either from a SAM line made by GraphMap or from the C++ Seqan output.
    '''
    def __init__(self,
                 sam_line=None, read_dict=None, reference_dict=None,
                 seqan_output=None, read=None, ref=None, rev_comp=None,
                 scoring_scheme=None):

        # Make sure we have the appropriate inputs for one of the two ways to construct an
        # alignment.
        assert (sam_line and read_dict and reference_dict) or \
               (seqan_output and read and ref)

        # The scoring scheme is required for both types of construction.
        assert scoring_scheme is not None

        # Read details
        self.read = None
        self.read_start_pos = None
        self.read_end_pos = None
        self.read_end_gap = None

        # Reference details
        self.ref = None
        self.ref_start_pos = None
        self.ref_end_pos = None
        self.ref_end_gap = None

        # Alignment details
        self.alignment_type = None
        self.rev_comp = None
        self.cigar = None
        self.cigar_parts = None
        self.match_count = None
        self.mismatch_count = None
        self.insertion_count = None
        self.deletion_count = None
        self.alignment_length = None
        self.edit_distance = None
        self.percent_identity = None
        self.ref_mismatch_positions = None
        self.ref_deletion_positions = None
        self.ref_insertion_positions_and_sizes = None
        self.raw_score = None
        self.scaled_score = None
        self.milliseconds = None

        # How some of the values are gotten depends on whether this alignment came from a GraphMap
        # SAM or a Seqan alignment.
        if seqan_output:
            self.setup_using_seqan_output(seqan_output, read, ref, rev_comp)
        elif sam_line:
            self.setup_using_graphmap_sam(sam_line, read_dict, reference_dict)

        self.tally_up_score_and_errors(scoring_scheme)

    def setup_using_seqan_output(self, seqan_output, read, ref, rev_comp):
        '''
        This function sets up the Alignment using the Seqan results. This kind of alignment has
        complete details about the alignment.
        '''
        self.alignment_type = 'Seqan'
        seqan_parts = seqan_output.split(',')
        assert len(seqan_parts) >= 6

        self.rev_comp = rev_comp
        self.cigar = seqan_parts[0]
        self.cigar_parts = re.findall(r'\d+\w', self.cigar)
        self.milliseconds = int(seqan_parts[5])

        self.read = read
        self.read_start_pos = int(seqan_parts[1])
        self.read_end_pos = int(seqan_parts[2])
        self.read_end_gap = self.read.get_length() - self.read_end_pos

        self.ref = ref
        self.ref_start_pos = int(seqan_parts[3])
        self.ref_end_pos = int(seqan_parts[4])
        self.ref_end_gap = len(self.ref.sequence) - self.ref_end_pos

    def setup_using_graphmap_sam(self, sam_line, read_dict, reference_dict):
        '''
        This function sets up the Alignment using a SAM line.
        '''
        self.alignment_type = 'GraphMap'
        sam_parts = sam_line.split('\t')
        self.rev_comp = bool(int(sam_parts[1]) & 0x10)
        self.cigar = sam_parts[5]
        self.cigar_parts = re.findall(r'\d+\w', self.cigar)

        self.read = read_dict[sam_parts[0]]
        self.read_start_pos = self.get_start_soft_clips()
        self.read_end_pos = self.read.get_length() - self.get_end_soft_clips()
        self.read_end_gap = self.get_end_soft_clips()

        self.ref = reference_dict[get_nice_header(sam_parts[2])]
        self.ref_start_pos = int(sam_parts[3]) - 1
        self.ref_end_pos = self.ref_start_pos
        for cigar_part in self.cigar_parts:
            self.ref_end_pos += get_ref_shift_from_cigar_part(cigar_part)

        # If all is good with the CIGAR, then we should never end up with a ref_end_pos out of the
        # reference range. But a CIGAR error (which has occurred in GraphMap) can cause this, so
        # check here.
        if self.ref_end_pos > len(self.ref.sequence):
            self.ref_end_pos = len(self.ref.sequence)

        self.ref_end_gap = len(self.ref.sequence) - self.ref_end_pos

    def tally_up_score_and_errors(self, scoring_scheme):
        '''
        This function steps through the CIGAR string for the alignment to get the score, identity
        and count/locations of errors.
        '''
        # Clear any existing tallies.
        self.match_count = 0
        self.mismatch_count = 0
        self.insertion_count = 0
        self.deletion_count = 0
        self.percent_identity = 0.0
        self.ref_mismatch_positions = []
        self.ref_deletion_positions = []
        self.ref_insertion_positions_and_sizes = []
        self.raw_score = 0

        # Remove the soft clipping parts of the CIGAR string for tallying.
        cigar_parts = self.cigar_parts[:]
        if cigar_parts[0][-1] == 'S':
            cigar_parts.pop(0)
        if cigar_parts and cigar_parts[-1][-1] == 'S':
            cigar_parts.pop()
        if not cigar_parts:
            return

        read_len = self.read.get_length()
        if self.rev_comp:
            read_seq = reverse_complement(self.read.sequence)
        else:
            read_seq = self.read.sequence
        read_i = self.read_start_pos

        ref_len = self.ref.get_length()
        ref_seq = self.ref.sequence
        ref_i = self.ref_start_pos
        align_i = 0

        for cigar_part in cigar_parts:
            cigar_count = int(cigar_part[:-1])
            cigar_type = cigar_part[-1]
            if cigar_type == 'I' or cigar_type == 'D':
                cigar_score = scoring_scheme.gap_open + \
                              ((cigar_count - 1) * scoring_scheme.gap_extend)
            if cigar_type == 'I':
                self.insertion_count += cigar_count
                self.ref_insertion_positions_and_sizes.append((ref_i, cigar_count))
                read_i += cigar_count
            elif cigar_type == 'D':
                self.deletion_count += cigar_count
                for i in xrange(cigar_count):
                    self.ref_deletion_positions.append(ref_i + i)
                ref_i += cigar_count
            else: # match/mismatch
                cigar_score = 0
                for _ in xrange(cigar_count):
                    # If all is good with the CIGAR, then we should never end up with a sequence
                    # index out of the sequence range. But a CIGAR error (which has occurred in
                    # GraphMap) can cause this, so check here.
                    if read_i >= read_len or ref_i >= ref_len:
                        break
                    read_base = read_seq[read_i]
                    ref_base = ref_seq[ref_i]
                    if read_base == ref_base:
                        self.match_count += 1
                        cigar_score += scoring_scheme.match
                    else:
                        self.mismatch_count += 1
                        self.ref_mismatch_positions.append(ref_i)
                        cigar_score += scoring_scheme.mismatch
                    read_i += 1
                    ref_i += 1

            self.raw_score += cigar_score
            align_i += cigar_count

        self.percent_identity = 100.0 * self.match_count / align_i
        self.edit_distance = self.mismatch_count + self.insertion_count + self.deletion_count
        self.alignment_length = align_i
        perfect_score = scoring_scheme.match * self.alignment_length
        self.scaled_score = 100.0 * self.raw_score / perfect_score

    def extend_start(self, scoring_scheme):
        '''
        This function extends the start of the alignment to remove any missing start bases.
        '''
        # Get the sequences to be realigned.
        missing_start_bases = self.get_missing_bases_at_start()
        realigned_bases = 2 * missing_start_bases
        realigned_read_end = self.read_start_pos
        realigned_read_start = max(0, realigned_read_end - realigned_bases)
        realigned_ref_end = self.ref_start_pos
        realigned_ref_start = max(0, realigned_ref_end - realigned_bases)
        if self.rev_comp:
            realigned_read_seq = \
                reverse_complement(self.read.sequence)[realigned_read_start:realigned_read_end]
        else:
            realigned_read_seq = self.read.sequence[realigned_read_start:realigned_read_end]
        realigned_ref_seq = self.ref.sequence[realigned_ref_start:realigned_ref_end]
        assert len(realigned_ref_seq) >= len(realigned_read_seq)

        if VERBOSITY > 3:
            print(self)
            print('   ', self.cigar[:20] + '...')
            cigar_length_before = len(self.cigar)

        # Call the C++ function to do the actual alignment.
        alignment_result = start_extension_alignment(realigned_read_seq, realigned_ref_seq,
                                                     scoring_scheme)

        seqan_parts = alignment_result.split(',')
        assert len(seqan_parts) >= 6

        # Set the new read start.
        self.read_start_pos = int(seqan_parts[1])
        assert self.read_start_pos == 0

        # Set the new reference start.
        self.ref_start_pos = realigned_ref_start + int(seqan_parts[3])

        # Replace the S part at the beginning the alignment's CIGAR with the CIGAR just made. If
        # the last part of the new CIGAR is of the same type as the first part of the existing
        # CIGAR, they will need to be merged.
        new_cigar_parts = re.findall(r'\d+\w', seqan_parts[0])
        old_cigar_parts = self.cigar_parts[1:]
        if new_cigar_parts[-1][-1] == old_cigar_parts[0][-1]:
            part_sum = int(new_cigar_parts[-1][:-1]) + int(old_cigar_parts[0][:-1])
            merged_part = str(part_sum) + new_cigar_parts[-1][-1]
            new_cigar_parts = new_cigar_parts[:-1] + [merged_part]
            old_cigar_parts = old_cigar_parts[1:]
        self.cigar_parts = new_cigar_parts + old_cigar_parts
        self.cigar = ''.join(self.cigar_parts)

        self.tally_up_score_and_errors(scoring_scheme)

        if VERBOSITY > 3:
            cigar_length_increase = len(self.cigar) - cigar_length_before
            print(self)
            print('    ', self.cigar[:(20 + cigar_length_increase)] + '...')
            print()

    def extend_end(self, scoring_scheme):
        '''
        This function extends the end of the alignment to remove any missing end bases.
        '''
        # Get the sequences to be realigned.
        missing_end_bases = self.get_missing_bases_at_end()
        realigned_bases = 2 * missing_end_bases
        realigned_read_start = self.read_end_pos
        realigned_read_end = min(self.read.get_length(), realigned_read_start + realigned_bases)
        realigned_ref_start = self.ref_end_pos
        realigned_ref_end = min(len(self.ref.sequence), realigned_ref_start + realigned_bases)
        if self.rev_comp:
            realigned_read_seq = \
                reverse_complement(self.read.sequence)[realigned_read_start:realigned_read_end]
        else:
            realigned_read_seq = self.read.sequence[realigned_read_start:realigned_read_end]
        realigned_ref_seq = self.ref.sequence[realigned_ref_start:realigned_ref_end]
        assert len(realigned_ref_seq) >= len(realigned_read_seq)

        if VERBOSITY > 3:
            print(self)
            print('    ...' + self.cigar[-20:])
            cigar_length_before = len(self.cigar)

        # Call the C++ function to do the actual alignment.
        alignment_result = end_extension_alignment(realigned_read_seq, realigned_ref_seq,
                                                   scoring_scheme)
        seqan_parts = alignment_result.split(',')
        assert len(seqan_parts) >= 6

        # Set the new read end.
        self.read_end_pos += int(seqan_parts[2])
        assert self.read_end_pos == self.read.get_length()
        self.read_end_gap = self.read.get_length() - self.read_end_pos

        # Set the new reference end.
        self.ref_end_pos += int(seqan_parts[4])
        self.ref_end_gap = len(self.ref.sequence) - self.ref_end_pos

        # Replace the S part at the end the alignment's CIGAR with the CIGAR just made. If
        # the first part of the new CIGAR is of the same type as the last part of the existing
        # CIGAR, they will need to be merged.
        old_cigar_parts = self.cigar_parts[:-1]
        new_cigar_parts = re.findall(r'\d+\w', seqan_parts[0])
        if old_cigar_parts[-1][-1] == new_cigar_parts[0][-1]:
            part_sum = int(old_cigar_parts[-1][:-1]) + int(new_cigar_parts[0][:-1])
            merged_part = str(part_sum) + new_cigar_parts[0][-1]
            old_cigar_parts = old_cigar_parts[:-1] + [merged_part]
            new_cigar_parts = new_cigar_parts[1:]
        self.cigar_parts = old_cigar_parts + new_cigar_parts
        self.cigar = ''.join(self.cigar_parts)

        self.tally_up_score_and_errors(scoring_scheme)

        if VERBOSITY > 3:
            cigar_length_increase = len(self.cigar) - cigar_length_before
            print(self)
            print('    ...' + self.cigar[-(20 + cigar_length_increase):])
            print()

    def __repr__(self):
        read_start, read_end = self.read_start_end_positive_strand()
        return_str = self.read.name + ' (' + str(read_start) + '-' + str(read_end) + ', '
        if self.rev_comp:
            return_str += 'strand: -), '
        else:
            return_str += 'strand: +), '
        return_str += self.ref.name + ' (' + str(self.ref_start_pos) + '-' + \
                      str(self.ref_end_pos) + ')'
        if self.percent_identity is not None:
            return_str += ', ' + '%.2f' % self.percent_identity + '% ID'
        if self.scaled_score is not None:
            return_str += ', raw score = ' + str(self.raw_score)
            return_str += ', scaled score = ' + '%.2f' % self.scaled_score
        return_str += ', longest indel: ' + str(self.get_longest_indel_run())
        if self.alignment_type == 'Seqan':
            return_str += ', ' + str(self.milliseconds) + ' ms'
        return return_str

    def get_ref_to_read_ratio(self):
        '''
        Returns the length ratio between the aligned parts of the reference and read.
        '''
        return (self.ref_end_pos - self.ref_start_pos) / (self.read_end_pos - self.read_start_pos)

    def get_read_to_ref_ratio(self):
        '''
        Returns the length ratio between the aligned parts of the read and reference.
        '''
        return 1.0 / self.get_ref_to_read_ratio()

    def read_start_end_positive_strand(self):
        '''
        This function returns the read start/end coordinates for the positive strand of the read.
        For alignments on the positive strand, this is just the normal start/end. But for
        alignments on the negative strand, the coordinates are flipped to the other side.
        '''
        if not self.rev_comp:
            return self.read_start_pos, self.read_end_pos
        else:
            start = self.read.get_length() - self.read_end_pos
            end = self.read.get_length() - self.read_start_pos
            return start, end

    def get_start_soft_clips(self):
        '''
        Returns the number of soft-clipped bases at the start of the alignment.
        '''
        if self.cigar_parts[0][-1] == 'S':
            return int(self.cigar_parts[0][:-1])
        else:
            return 0

    def get_end_soft_clips(self):
        '''
        Returns the number of soft-clipped bases at the start of the alignment.
        '''
        if self.cigar_parts[-1][-1] == 'S':
            return int(self.cigar_parts[-1][:-1])
        else:
            return 0

    def get_sam_line(self):
        '''
        Returns a SAM alignment line.
        '''
        edit_distance = self.mismatch_count + self.insertion_count + self.deletion_count
        sam_flag = 0 #TO DO: SET THIS PROPERLY
        return '\t'.join([self.read.name, str(sam_flag), self.ref.name,
                          str(self.ref_start_pos + 1), '255', self.cigar,
                          '*', '0', '0', self.read.sequence, self.read.qualities,
                          'NM:i:' + str(edit_distance), 'AS:i:' + str(self.raw_score)])

    def is_whole_read(self):
        '''
        Returns True if the alignment covers the entirety of the read.
        '''
        return self.read_start_pos == 0 and self.read_end_gap == 0

    def get_longest_indel_run(self):
        '''
        Returns the longest indel in the alignment.
        '''
        longest_indel_run = 0
        for cigar_part in self.cigar_parts:
            cigar_type = cigar_part[-1]
            if cigar_type == 'I' or cigar_type == 'D':
                longest_indel_run = max(longest_indel_run, int(cigar_part[:-1]))
        return longest_indel_run

    def get_missing_bases_at_start(self):
        '''
        Returns the number of bases at the start of the alignment which are missing in both the
        read and the reference (preventing the alignment from being semi-global).
        '''
        return min(self.read_start_pos, self.ref_start_pos)

    def get_missing_bases_at_end(self):
        '''
        Returns the number of bases at the end of the alignment which are missing in both the read
        and the reference (preventing the alignment from being semi-global).
        '''
        return min(self.read_end_gap, self.ref_end_gap)

    def get_total_missing_bases(self):
        '''
        Returns the number of bases at the start and end of the alignment which are missing in both
        the read and the reference (preventing the alignment from being semi-global).
        '''
        return self.get_missing_bases_at_start() + self.get_missing_bases_at_end()




'''
This is the big semi-global C++ Seqan alignment function.
'''
C_LIB.semiGlobalAlignment.argtypes = [c_char_p, # Read name
                                      c_char_p, # Read sequence
                                      c_char_p, # Reference name
                                      c_char_p, # Reference sequence
                                      c_double, # Expected slope
                                      c_int,    # Verbosity
                                      c_void_p, # KmerPositions pointer
                                      c_int,    # Match score
                                      c_int,    # Mismatch score
                                      c_int,    # Gap open score
                                      c_int]    # Gap extension score
C_LIB.semiGlobalAlignment.restype = c_void_p    # String describing alignments


'''
These functions are used to conduct short alignments for the sake of extending the start and end of
a GraphMap alignment.
'''
C_LIB.startExtensionAlignment.argtypes = [c_char_p, # Read sequence
                                          c_char_p, # Reference sequence
                                          c_int,    # Read sequence length
                                          c_int,    # Reference sequence length
                                          c_int,    # Verbosity
                                          c_int,    # Match score
                                          c_int,    # Mismatch score
                                          c_int,    # Gap open score
                                          c_int]    # Gap extension score
C_LIB.startExtensionAlignment.restype = c_void_p    # String describing alignment

C_LIB.endExtensionAlignment.argtypes = [c_char_p, # Read sequence
                                        c_char_p, # Reference sequence
                                        c_int,    # Read sequence length
                                        c_int,    # Reference sequence length
                                        c_int,    # Verbosity
                                        c_int,    # Match score
                                        c_int,    # Mismatch score
                                        c_int,    # Gap open score
                                        c_int]    # Gap extension score
C_LIB.endExtensionAlignment.restype = c_void_p    # String describing alignment


def start_extension_alignment(realigned_read_seq, realigned_ref_seq, scoring_scheme):
    '''
    Python wrapper for startExtensionAlignment C++ function.
    '''
    ptr = C_LIB.startExtensionAlignment(realigned_read_seq, realigned_ref_seq,
                                        len(realigned_read_seq), len(realigned_ref_seq),
                                        VERBOSITY,
                                        scoring_scheme.match, scoring_scheme.mismatch,
                                        scoring_scheme.gap_open, scoring_scheme.gap_extend)
    return c_string_to_python_string(ptr)

def end_extension_alignment(realigned_read_seq, realigned_ref_seq, scoring_scheme):
    '''
    Python wrapper for endExtensionAlignment C++ function.
    '''
    ptr = C_LIB.endExtensionAlignment(realigned_read_seq, realigned_ref_seq,
                                      len(realigned_read_seq), len(realigned_ref_seq),
                                      VERBOSITY,
                                      scoring_scheme.match, scoring_scheme.mismatch,
                                      scoring_scheme.gap_open, scoring_scheme.gap_extend)
    return c_string_to_python_string(ptr)


'''
This function cleans up the heap memory for the C strings returned by the other C functions. It
must be called after them.
'''
C_LIB.freeCString.argtypes = [c_void_p]
C_LIB.freeCString.restype = None

def c_string_to_python_string(c_string):
    '''
    This function casts a C string to a Python string and then calls a function to delete the C
    string from the heap.
    '''
    python_string = cast(c_string, c_char_p).value    
    C_LIB.freeCString(c_string)
    return python_string


'''
These functions make/delete a C++ object that will be used during line-finding.
'''
C_LIB.newKmerPositions.argtypes = []
C_LIB.newKmerPositions.restype = c_void_p
C_LIB.addKmerPositions.argtypes = [c_void_p, # KmerPositions pointer
                                   c_char_p, # Name
                                   c_char_p] # Sequence
C_LIB.addKmerPositions.restype = None
C_LIB.deleteKmerPositions.argtypes = [c_void_p, # KmerPositions pointer
                                      c_char_p] # Name
C_LIB.deleteKmerPositions.restype = None
C_LIB.deleteAllKmerPositions.argtypes = [c_void_p]
C_LIB.deleteAllKmerPositions.restype = None

def add_read_kmer_positions(kmer_positions_ptr, read):
    '''
    Adds both positive and negative sequences of a read to KmerPositions.
    '''
    C_LIB.addKmerPositions(kmer_positions_ptr, read.name + '+', read.sequence)
    C_LIB.addKmerPositions(kmer_positions_ptr, read.name + '-', reverse_complement(read.sequence))

def delete_read_kmer_positions(kmer_positions_ptr, read):
    '''
    Adds both positive and negative sequences of a read to KmerPositions.
    '''
    C_LIB.deleteKmerPositions(kmer_positions_ptr, read.name + '+')
    C_LIB.deleteKmerPositions(kmer_positions_ptr, read.name + '-')



if __name__ == '__main__':
    main()
