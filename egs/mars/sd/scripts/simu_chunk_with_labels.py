import logging
import numpy as np
import soundfile
import kaldiio
from funasr.utils.job_runner import MultiProcessRunnerV3
from funasr.utils.misc import load_scp_as_list, load_scp_as_dict
import os
import argparse
from collections import OrderedDict
import random
from typing import List, Dict
from copy import deepcopy
import json
logging.basicConfig(
    level="INFO",
    format=f"[{os.uname()[1].split('.')[0]}]"
           f" %(asctime)s (%(module)s:%(lineno)d) %(levelname)s: %(message)s",
)


class MyRunner(MultiProcessRunnerV3):

    def prepare(self, parser: argparse.ArgumentParser):
        parser.add_argument("--label_scp", type=str, required=True)
        parser.add_argument("--wav_scp", type=str, required=True)
        parser.add_argument("--utt2spk", type=str, required=True)
        parser.add_argument("--spk2meeting", type=str, required=True)
        parser.add_argument("--utt2xvec", type=str, required=True)
        parser.add_argument("--out_dir", type=str, required=True)
        parser.add_argument("--chunk_size", type=float, default=16)
        parser.add_argument("--chunk_shift", type=float, default=4)
        parser.add_argument("--frame_shift", type=float, default=0.01)
        parser.add_argument("--embedding_dim", type=int, default=None)
        parser.add_argument("--average_emb_num", type=int, default=0)
        parser.add_argument("--subset", type=int, default=0)
        parser.add_argument("--data_json", type=str, default=None)
        parser.add_argument("--seed", type=int, default=1234)
        parser.add_argument("--log_interval", type=int, default=100)
        args = parser.parse_args()
        random.seed(args.seed)
        np.random.seed(args.seed)

        logging.info("Loading data...")
        if not os.path.exists(args.data_json):
            label_list = load_scp_as_list(args.label_scp)
            wav_scp = load_scp_as_dict(args.wav_scp)
            utt2spk = load_scp_as_dict(args.utt2spk)
            utt2xvec = load_scp_as_dict(args.utt2xvec)
            spk2meeting = load_scp_as_dict(args.spk2meeting)

            meeting2spks = OrderedDict()
            for spk, meeting in spk2meeting.items():
                if meeting not in meeting2spks:
                    meeting2spks[meeting] = []
                meeting2spks[meeting].append(spk)

            spk2utts = OrderedDict()
            for utt, spk in utt2spk.items():
                if spk not in spk2utts:
                    spk2utts[spk] = []
                spk2utts[spk].append(utt)

            os.makedirs(os.path.dirname(args.data_json), exist_ok=True)
            logging.info("Dump data...")
            json.dump({
                "label_list": label_list, "wav_scp": wav_scp, "utt2xvec": utt2xvec,
                "spk2utts": spk2utts, "meeting2spks": meeting2spks
            }, open(args.data_json, "wt", encoding="utf-8"), ensure_ascii=False, indent=4)
        else:
            data_dict = json.load(open(args.data_json, "rt", encoding="utf-8"))
            label_list = data_dict["label_list"]
            wav_scp = data_dict["wav_scp"]
            utt2xvec = data_dict["utt2xvec"]
            spk2utts = data_dict["spk2utts"]
            meeting2spks = data_dict["meeting2spks"]

        if not os.path.exists(args.out_dir):
            os.makedirs(args.out_dir)

        args.chunk_size = int(args.chunk_size / args.frame_shift)
        args.chunk_shift = int(args.chunk_shift / args.frame_shift)

        if args.embedding_dim is None:
            args.embedding_dim = kaldiio.load_mat(next(iter(utt2xvec.values()))).shape[1]
            logging.info("Embedding dim is detected as {}.".format(args.embedding_dim))

        logging.info("Number utt: {}, Number speaker: {}, Number meetings: {}".format(
            len(wav_scp), len(spk2utts), len(meeting2spks)
        ))
        return label_list, (wav_scp, utt2xvec, spk2utts, meeting2spks), args

    def post(self, results_list, args):
        logging.info("[main]: Got {} chunks.".format(sum(results_list)))


def simu_wav_chunk(spk, spk2utts, wav_scp, sample_length):
    utt_list = spk2utts[spk]
    wav_list = []
    cur_length = 0
    while cur_length < sample_length:
        uttid = random.choice(utt_list)
        wav, fs = soundfile.read(wav_scp[uttid], dtype='float32')
        wav_list.append(wav)
        cur_length += len(wav)
    concat_wav = np.concatenate(wav_list, axis=0)
    start = random.randint(0, len(concat_wav) - sample_length)
    return concat_wav[start: start+sample_length]


def calculate_embedding(spk, spk2utts, utt2xvec, embedding_dim, average_emb_num):
    # process for dummy speaker
    if spk == "None":
        return np.zeros((1, embedding_dim), dtype=np.float32)

    # calculate averaged speaker embeddings
    utt_list = spk2utts[spk]
    if average_emb_num == 0 or average_emb_num > len(utt_list):
        xvec_list = [kaldiio.load_mat(utt2xvec[utt]) for utt in utt_list]
    else:
        xvec_list = [kaldiio.load_mat(utt2xvec[utt]) for utt in random.sample(utt_list, average_emb_num)]
    xvec = np.concatenate(xvec_list, axis=0)
    xvec = xvec / np.linalg.norm(xvec, axis=-1, keepdims=True)
    xvec = np.mean(xvec, axis=0)

    return xvec


def simu_chunk(
        frame_label: np.ndarray,
        sample_label: np.ndarray,
        wav_scp: Dict[str, str],
        utt2xvec: Dict[str, str],
        spk2utts: Dict[str, List[str]],
        meeting2spks: Dict[str, List[str]],
        all_speaker_list: List[str],
        meeting_list: List[str],
        embedding_dim: int,
        average_emb_num: int,
):
    frame_length, max_spk_num = frame_label.shape
    sample_length = sample_label.shape[0]
    positive_speaker_num = int(np.sum(frame_label.sum(axis=0) > 0))
    pos_speaker_list = deepcopy(meeting2spks[random.choice(meeting_list)])

    # get positive speakers
    if len(pos_speaker_list) >= positive_speaker_num:
        pos_speaker_list = random.sample(pos_speaker_list, positive_speaker_num)
    else:
        while len(pos_speaker_list) < positive_speaker_num:
            _spk = random.choice(all_speaker_list)
            if _spk not in pos_speaker_list:
                pos_speaker_list.append(_spk)

    # get negative speakers
    negative_speaker_num = random.randint(0, max_spk_num - positive_speaker_num)
    neg_speaker_list = []
    while len(neg_speaker_list) < negative_speaker_num:
        _spk = random.choice(all_speaker_list)
        if _spk not in pos_speaker_list and _spk not in neg_speaker_list:
            neg_speaker_list.append(_spk)
    neg_speaker_list.extend(["None"] * (max_spk_num - positive_speaker_num - negative_speaker_num))

    random.shuffle(pos_speaker_list)
    random.shuffle(neg_speaker_list)
    seperated_wav = np.zeros(sample_label.shape, dtype=np.float32)
    this_spk_list = []
    for idx, frame_num in enumerate(frame_label.sum(axis=0)):
        if frame_num > 0:
            spk = pos_speaker_list.pop(0)
            this_spk_list.append(spk)
            simu_spk_wav = simu_wav_chunk(spk, spk2utts, wav_scp, sample_length)
            seperated_wav[:, idx] = simu_spk_wav
        else:
            spk = neg_speaker_list.pop(0)
            this_spk_list.append(spk)

    # calculate mixed wav
    mixed_wav = np.sum(seperated_wav * sample_label, axis=1)

    # shuffle the order of speakers
    shuffle_idx = list(range(max_spk_num))
    random.shuffle(shuffle_idx)
    this_spk_list = [this_spk_list[x] for x in shuffle_idx]
    seperated_wav = seperated_wav.transpose()[shuffle_idx].transpose()
    frame_label = frame_label.transpose()[shuffle_idx].transpose()

    # calculate profile
    profile = [calculate_embedding(spk, spk2utts, utt2xvec, embedding_dim, average_emb_num)
               for spk in this_spk_list]
    profile = np.vstack(profile)
    # pse_weights = 2 ** np.arange(max_spk_num)
    # pse_label = np.sum(frame_label * pse_weights[np.newaxis, :], axis=1)
    # pse_label = pse_label.astype(str).tolist()

    return mixed_wav, seperated_wav, profile, frame_label


def process(task_args):
    task_idx, task_list, (wav_scp, utt2xvec, spk2utts, meeting2spks), args = task_args
    logging.info("{:02d}/{:02d}: Start simulation...".format(task_idx+1, args.nj))

    out_path = os.path.join(args.out_dir, "wav_mix.{}".format(task_idx+1))
    wav_mix_writer = kaldiio.WriteHelper('ark,scp:{}.ark,{}.scp'.format(out_path, out_path))

    # out_path = os.path.join(args.out_dir, "wav_sep.{}".format(task_idx + 1))
    # wav_sep_writer = kaldiio.WriteHelper('ark,scp:{}.ark,{}.scp'.format(out_path, out_path))

    out_path = os.path.join(args.out_dir, "profile.{}".format(task_idx + 1))
    profile_writer = kaldiio.WriteHelper('ark,scp:{}.ark,{}.scp'.format(out_path, out_path))

    out_path = os.path.join(args.out_dir, "frame_label.{}".format(task_idx + 1))
    label_writer = kaldiio.WriteHelper('ark,scp:{}.ark,{}.scp'.format(out_path, out_path))

    speaker_list, meeting_list = list(spk2utts.keys()), list(meeting2spks.keys())

    labels_list = []
    total_chunks = 0
    for org_mid, label_path in task_list:
        whole_label = kaldiio.load_mat(label_path)
        # random offset to keep diversity
        rand_shift = random.randint(0, args.chunk_shift)
        num_chunk = (whole_label.shape[0] - rand_shift - args.chunk_size) // args.chunk_shift + 1
        labels_list.append((org_mid, whole_label, rand_shift, num_chunk))
        total_chunks += num_chunk

    idx = 0
    simu_chunk_count = 0
    for org_mid, whole_label, rand_shift, num_chunk in labels_list:
        for i in range(num_chunk):
            idx = idx + 1
            st = i * args.chunk_shift + rand_shift
            ed = i * args.chunk_shift + args.chunk_size + rand_shift
            utt_id = "subset{}_part{}_{}_{:06d}_{:06d}".format(
                args.subset + 1, task_idx + 1, org_mid, st, ed
            )
            frame_label = whole_label[st: ed, :]
            sample_label = frame_label.repeat(int(args.sr * args.frame_shift), axis=0)
            mix_wav, seg_wav, profile, frame_label = simu_chunk(
                frame_label, sample_label, wav_scp, utt2xvec, spk2utts, meeting2spks,
                speaker_list, meeting_list, args.embedding_dim, args.average_emb_num
            )
            wav_mix_writer(utt_id, mix_wav)
            # wav_sep_writer(utt_id, seg_wav)
            profile_writer(utt_id, profile)
            label_writer(utt_id, frame_label)

            simu_chunk_count += 1
            if simu_chunk_count % args.log_interval == 0:
                logging.info("{:02d}/{:02d}: Complete {}/{} simulation, {}.".format(
                    task_idx + 1, args.nj, simu_chunk_count, total_chunks, utt_id))
    wav_mix_writer.close()
    # wav_sep_writer.close()
    profile_writer.close()
    label_writer.close()
    logging.info("[{}/{}]: Simulate {} chunks.".format(task_idx+1, args.nj, simu_chunk_count))
    return simu_chunk_count


if __name__ == '__main__':
    my_runner = MyRunner(process)
    my_runner.run()
