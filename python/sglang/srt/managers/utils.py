import json
import logging
import time
from collections import defaultdict
from http import HTTPStatus
from typing import Dict, List, Optional, Tuple

import torch

from sglang.srt.managers.schedule_batch import FINISH_ABORT, Req

logger = logging.getLogger(__name__)


def validate_input_length(
    req: Req, max_req_input_len: int, allow_auto_truncate: bool
) -> Optional[str]:
    """Validate and potentially truncate input length.

    Args:
        req: The request containing input_ids to validate
        max_req_input_len: Maximum allowed input length
        allow_auto_truncate: Whether to truncate long inputs

    Returns:
        Error message if validation fails, None if successful
    """
    if len(req.origin_input_ids) >= max_req_input_len:
        if allow_auto_truncate:
            logger.warning(
                "Request length is longer than the KV cache pool size or "
                "the max context length. Truncated. "
                f"{len(req.origin_input_ids)=}, {max_req_input_len=}."
            )
            req.origin_input_ids = req.origin_input_ids[:max_req_input_len]
            return None
        else:
            error_msg = (
                f"Input length ({len(req.origin_input_ids)} tokens) exceeds "
                f"the maximum allowed length ({max_req_input_len} tokens). "
                f"Use a shorter input or enable --allow-auto-truncate."
            )
            logger.error(error_msg)
            req.finished_reason = FINISH_ABORT(
                error_msg, HTTPStatus.BAD_REQUEST, "BadRequestError"
            )
            return error_msg

    return None


# global expert distribution recording
class ExpertDistributionRecorder:
    # This class is a singleton class
    def __new__(cls):
        if not hasattr(cls, "instance"):
            cls.instance = super(ExpertDistributionRecorder, cls).__new__(cls)
        return cls.instance

    def __init__(self):
        # the length of the dictionary is the number of layers
        # the length of the list is the number of tokens
        # the length of the tuple is topk's k value
        self._expert_distribution_record: Dict[int, List[Tuple[int]]] = defaultdict(
            list
        )
        self._record = False
        self._current_layer_id = "UNKNOWN"

    def set_current_layer(self, layer_idx):
        self._current_layer_id = layer_idx

    def record_new_token(self, topk_ids):
        if not self._record:
            return
        topk_ids_list = topk_ids.to("cpu", non_blocking=True).numpy().tolist()
        torch.cuda.synchronize()
        for i in topk_ids_list:
            self._expert_distribution_record[self._current_layer_id].append(tuple(i))

    def reset(self):
        """Reset the expert distribution recorder."""
        logger.info("Resetting expert distribution record...")
        self._record = False
        self._expert_distribution_record.clear()
        self._current_layer_id = "UNKNOWN"

    def start_record(self):
        """Start recording the expert distribution. Reset the recorder and set the recording flag to True."""
        if self._record == True:
            logger.warning(
                "SGLang server is already recording expert ids. Did you forget to dump the expert ids recorded so far by sending requests to the `/stop_expert_distribution_record` and `/dump_expert_distribution_record` endpoints?"
            )
        self.reset()
        self._record = True

    def stop_record(self):
        """Stop recording the expert distribution. Set the recording flag to False."""
        if self._record == False:
            logger.warning(
                "SGLang server has not been recording expert ids. Did you forget to start recording by sending request to the `/start_expert_distribution_record` endpoint?"
            )
        self._record = False

    def dump_record(self):
        """Dump the expert distribution record to a file. Reset the recorder after dumping."""
        results = {}
        for layer_idx, layer_record in self._expert_distribution_record.items():
            results[layer_idx] = defaultdict(int)
            for token_record in layer_record:
                for expert_idx in token_record:
                    results[layer_idx][expert_idx] += 1
        with open(
            f"expert_distribution_rank{torch.distributed.get_rank()}_timestamp{time.time()}.csv",
            "w",
        ) as fd:
            fd.write("layer_id,expert_id,count\n")
            for layer_idx, layer_results in results.items():
                for expert_idx, count in layer_results.items():
                    fd.write(f"{layer_idx},{expert_idx},{count}\n")
        self.reset()
