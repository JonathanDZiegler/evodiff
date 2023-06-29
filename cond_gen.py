from dms.pretrained import OA_AR_640M, OA_AR_38M
import numpy as np
import argparse
import urllib.request
import esm.inverse_folding
from sequence_models.constants import MSA_ALPHABET, ALL_AAS, PROTEIN_ALPHABET, PAD
import torch
import os
import glob
import json
from dms.utils import Tokenizer
import pathlib
from sequence_models.datasets import UniRefDataset
from sequence_models.utils import parse_fasta
from torch.utils.data import DataLoader
from torch.utils.data import Subset
from tqdm import tqdm
from analysis.plot import aa_reconstruction_parity_plot
import pandas as pd
from sequence_models.samplers import SortishSampler, ApproxBatchSampler
import random

# python cond_gen.py --cond-task scaffold --pdb 5trv --motif-start-index 42 --motif-end-index 62 --num-seqs 50

home = str(pathlib.Path.home())

def main():
    # set seeds
    _ = torch.manual_seed(0)
    np.random.seed(0)

    parser = argparse.ArgumentParser()
    parser.add_argument('--model-type', type=str, default='oa_ar_640M',
                        help='Choice of: carp_38M carp_640M esm1b_640M \
                              oa_ar_38M oa_ar_640M')
    parser.add_argument('--gpus', type=int, default=0)
    parser.add_argument('--cond-task', type=str, default='scaffold',
                        help="Choice of 'scaffold' or 'idr'")
    parser.add_argument('--pdb', type=str, default=None,
                        help="If using cond-task=scaffold, provide a PDB code and motif indexes")
    #parser.add_argument('--motif_idxs', type=list, default=[(0,10)])
    parser.add_argument('--start-idxs', type=int, action='append')
    parser.add_argument('--end-idxs', type=int, action='append')
    # parser.add_argument('--motif-start-index', type=int, default=0,
    #                     help="If using cond-task=scaffold, provide start and end indexes for motif being scaffolded")
    # parser.add_argument('--motif-end-index', type=int, default=0,
    #                   help="If using cond-task=scaffold, provide start and end indexes for motif being scaffolded")
    parser.add_argument('--num-seqs', type=int, default=10,
                        help="Number of sequences generated per scaffold length")
    parser.add_argument('--scaffold-min', type=int, default=1,
                        help="Min scaffold len ")
    parser.add_argument('--scaffold-max', type=int, default=30,
                        help="Max scaffold len, will randomly choose a value between min/max")
    args = parser.parse_args()

    if args.model_type == 'oa_ar_38M':
        checkpoint = OA_AR_38M()
    elif args.model_type == 'oa_ar_640M':
        checkpoint = OA_AR_640M()
    else:
        print("Please select valid model")

    model, collater, tokenizer, scheme = checkpoint
    model.eval().cuda()

    torch.cuda.set_device(args.gpus)
    device = torch.device('cuda:' + str(args.gpus))

    data_top_dir = home + '/Desktop/DMs/data/'

    out_fpath = home + '/Desktop/DMs/cond-gen/' + args.pdb +'/'
    if not os.path.exists(out_fpath):
        os.mkdir(out_fpath)

    if args.cond_task == 'idr':
        #TODO Finish IDR
        sample, string, queries, sequences = generate_idr(model, data_top_dir, tokenizer=tokenizer,
                                                          penalty=args.penalty, batch_size=1, device=device)
    elif args.cond_task == 'scaffold':
        strings = []
        start_idxs = []
        scaffold_lengths = []
        spacers = []
        for i in range(args.num_seqs):
            scaffold_length = random.randint(args.scaffold_min, args.scaffold_max)
            #print("scaffold length", scaffold_length)
            #print("motif length", args.motif_end_index+1-args.motif_start_index)
            print(args.start_idxs)
            print(args.end_idxs)
            string, new_start_idx, spacer = generate_scaffold(model, args.pdb, args.start_idxs, args.end_idxs, scaffold_length, data_top_dir,
                                       tokenizer, device=device)
            #print(string, new_start_idx)
            strings.append(string)
            start_idxs.append(new_start_idx)
            spacers.append(spacer)
            scaffold_lengths.append(scaffold_length)
    #print(strings)

    save_df = pd.DataFrame(list(zip(strings, start_idxs, spacers, scaffold_lengths)), columns=['seqs', 'start_idxs', 'spacers', 'scaffold_lengths'])
    save_df.to_csv(out_fpath+'motif_df.csv', index=True)

    with open(out_fpath + 'generated_samples_string.csv', 'a') as f:
        for _s in strings:
            #print(_s[0])
            f.write(_s[0]+"\n")
    with open(out_fpath + 'generated_samples_string.fasta', 'a') as f:
        for i, _s in enumerate(strings):
            f.write(">SEQUENCE_" + str(i) + "\n" + str(_s[0]) + "\n")

def download_pdb(PDB_ID, outfile):
    "return PDB file from database online"
    if os.path.exists(outfile):
        print("ALREADY DOWNLOADED")
    else:
        url = 'https://files.rcsb.org/download/'+str(PDB_ID)+'.pdb'
        print("DOWNLOADING PDB FILE FROM", url)
        urllib.request.urlretrieve(url, outfile)

def get_motif(PDB_ID, start_idxs, end_idxs, data_top_dir='../data'):
    "Get motif of sequence from PDB code"
    pdb_path = os.path.join(data_top_dir, 'scaffolding-pdbs/'+str(PDB_ID)+'.pdb')
    download_pdb(PDB_ID, pdb_path)

    chain_ids = ['A']
    print("WARNING: ASSUMING ONLY 1 CHAIN IN PDB FILE")
    structure = esm.inverse_folding.util.load_structure(pdb_path, chain_ids)
    coords, native_seqs = esm.inverse_folding.multichain_util.extract_coords_from_complex(structure)
    sequence = native_seqs[chain_ids[0]]
    print("sequence extracted from pdb", sequence)
    print("sequence length", len(sequence))
    assert len(start_idxs) == len(end_idxs)

    end_idxs = [i+1 for i in end_idxs] # inclusive of final residue
    if len(start_idxs) > 1:
        #print(min(start_idxs))
        #print(max(end_idxs))
        motif = ''
        spacers = []
        print("start idxs", start_idxs)
        print("end idxs", end_idxs)
        for i in range(len(start_idxs)):
            motif += sequence[start_idxs[i]:end_idxs[i]]
            if i < (len(start_idxs)-1):
                #print('#' * len(sequence[end_idxs[i]:start_idxs[i+1]]))
                print(start_idxs[i+1], end_idxs[i])
                spacer = start_idxs[i+1] - end_idxs[i]
                print(spacer)
                motif += '#' * spacer
                spacers.append(spacer)
    else:
        motif = sequence[start_idxs[0]: end_idxs[0]]
        spacers=[0]
    return motif, spacers


def generate_scaffold(model, PDB_ID, motif_start_idxs, motif_end_idxs, scaffold_length, data_top_dir, tokenizer,
                      batch_size=1, device='gpu'):
    mask = tokenizer.mask_id

    motif_seq, spacers = get_motif(PDB_ID, motif_start_idxs, motif_end_idxs, data_top_dir=data_top_dir)
    motif_tokenized = tokenizer.tokenize((motif_seq,))
    #print("motif tokenized", motif_tokenized)

    # Create input motif + scaffold
    seq_len = scaffold_length + len(motif_seq)
    sample = torch.zeros((batch_size, seq_len)) + mask # start from all mask
    new_start = np.random.choice(scaffold_length) # randomly place motif in scaffold
    sample[:, new_start:new_start+len(motif_seq)] = torch.tensor(motif_tokenized)
    print([tokenizer.untokenize(s) for s in sample])
    print("new start", new_start, "new end", new_start +len(motif_seq))
    value, loc = (sample == mask).long().nonzero(as_tuple=True) # locations that need to be unmasked
    loc = np.array(loc)
    #print(loc)
    np.random.shuffle(loc)
    #print(loc)
    sample = sample.long().to(device)
    with torch.no_grad():
        for i in loc:
            timestep = torch.tensor([0] * batch_size)  # placeholder but not called in model
            timestep = timestep.to(device)
            prediction = model(sample, timestep)
            p = prediction[:, i, :len(tokenizer.all_aas) - 6]  # only canonical
            p = torch.nn.functional.softmax(p, dim=1)  # softmax over categorical probs
            p_sample = torch.multinomial(p, num_samples=1)
            sample[:, i] = p_sample.squeeze()

    print("new sequence", [tokenizer.untokenize(s) for s in sample])
    untokenized = [tokenizer.untokenize(s) for s in sample]

    return untokenized, new_start, spacers

def generate_idr(model, data_top_dir, tokenizer=Tokenizer(), penalty=None, causal=False, batch_size=20, device='cuda'):
    cutoff = 256 # TODO ADD FILTER

    all_aas = tokenizer.all_aas
    tokenized_sequences, start_idxs, end_idxs, queries, sequences = get_IDR_sequences(data_top_dir, tokenizer)
    samples = []
    samples_idr = []
    sequences_idr = []
    for s, sample in enumerate(tokenized_sequences):
        loc = np.arange(start_idxs[s], end_idxs[s])
        if len(loc) < cutoff:
            print("QUERY", queries[s])
            #print("ORIGINAL SEQUENCE", sequences[s])
            print("ORIGINAL IDR", sequences[s][start_idxs[s]:end_idxs[s]])
            sequences_idr.append(sequences[s][start_idxs[s]:end_idxs[s]])
            sample = sample.to(torch.long)
            sample = sample.to(device)
            seq_len = len(sample)
            #print(start_idxs[s], end_idxs[s])
            if causal == False:
                np.random.shuffle(loc)
            with torch.no_grad():
                for i in tqdm(loc):
                    timestep = torch.tensor([0]) # placeholder but not called in model
                    timestep = timestep.to(device)
                    prediction = model(sample.unsqueeze(0), timestep) #, input_mask=input_mask.unsqueeze(-1)) #sample prediction given input
                    p = prediction[:, i, :len(all_aas)-6] # sample at location i (random), dont let it predict non-standard AA
                    p = torch.nn.functional.softmax(p, dim=1) # softmax over categorical probs
                    p_sample = torch.multinomial(p, num_samples=1)
                    # Repetition penalty
                    if penalty is not None: # ignore if value is None
                        for j in range(batch_size): # iterate over each obj in batch
                            case1 = (i == 0 and sample[j, i+1] == p_sample[j]) # beginning of seq
                            case2 = (i == seq_len-1 and sample[j, i-1] == p_sample[j]) # end of seq
                            case3 = ((i < seq_len-1 and i > 0) and ((sample[j, i-1] == p_sample[j]) or (sample[j, i+1] == p_sample[j]))) # middle of seq
                            if case1 or case2 or case3:
                                p[j, int(p_sample[j])] /= penalty # reduce prob of that token by penalty value
                                p_sample[j] = torch.multinomial(p[j], num_samples=1) # resample
                    sample[i] = p_sample.squeeze()
                    #print(tokenizer.untokenize(sample))
            #print("GENERATED SEQUENCES", tokenizer.untokenize(sample))
            print("GENERATED IDR", tokenizer.untokenize(sample[start_idxs[s]:end_idxs[s]]))
            samples.append(sample)
            samples_idr.append(sample[start_idxs[s]:end_idxs[s]])
        else:
            pass
    #untokenized = [tokenizer.untokenize(s) for s in samples]
    untokenized_idr = [tokenizer.untokenize(s) for s in samples_idr]
    return samples, untokenized_idr, queries, sequences_idr

def get_IDR_sequences(data_top_dir, tokenizer):
    sequences = []
    masked_sequences = []
    start_idxs = []
    end_idxs = []
    queries = []
    # GET IDRS
    data_dir = data_top_dir + 'human_idr_alignments/'
    all_files = os.listdir(data_dir + 'human_protein_alignments')
    index_file = pd.read_csv(data_dir + 'human_idr_boundaries.tsv', delimiter='\t')
    print(len(index_file), "TOTAL IDRS")
    for index, row in index_file[:50].iterrows(): # TODO only iterating over 100 right now
        msa_file = [file for i, file in enumerate(all_files) if row['OMA_ID'] in file][0]
        msa_data, msa_names = parse_fasta(data_dir + 'human_protein_alignments/' + msa_file, return_names=True)
        query_idx = [i for i, name in enumerate(msa_names) if name == row['OMA_ID']][0]  # get query index
        queries.append(row['OMA_ID'])
        # JUST FOR SEQUENCES
        #print("IDR:\n", row['IDR_SEQ'])
        #print("MSA IDR NO GAPS:\n", msa_data[query_idx].replace("-", ""))
        seq_only = msa_data[query_idx].replace("-", "")
        sequences.append(seq_only)
        start_idx = row['START'] - 1
        end_idx = row['END']
        idr_range = end_idx - start_idx
        #print(start_idx, end_idx, idr_range)
        masked_sequence = seq_only[0:start_idx] + '#' * idr_range + seq_only[end_idx:]
        #print("MASKED SEQUENCE:\n", masked_sequence)
        masked_sequences.append(masked_sequence)
        start_idxs.append(start_idx)
        end_idxs.append(end_idx)
    tokenized = [torch.tensor(tokenizer.tokenizeMSA(s)) for s in masked_sequences]
    #print(tokenized[0])
    return tokenized, start_idxs, end_idxs, queries, sequences

if __name__ == '__main__':
    main()