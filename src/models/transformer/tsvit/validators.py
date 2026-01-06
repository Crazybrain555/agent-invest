from typing import Literal


def decide_encoder_impl(
    encoder_impl: Literal['auto','native','custom'],
    pos_encoding: Literal['none','abs','rope','rpb','rope_rpb','sinus'],
    drop_path_rate: float
):
    """返回 ('native'|'custom'), 并在非法组合上抛错。

    规则:
      - encoder_impl='native':
          * 禁用 RoPE/RPB（仅允许 none/abs/sinus）
          * 要求 drop_path_rate==0
      - encoder_impl='custom':
          * 均可（RoPE/RPB 仅在 attention 内生效）
      - encoder_impl='auto':
          * (pos in {none,abs,sinus}) 且 drop_path_rate==0 → native
          * 否则 → custom
    """
    if encoder_impl == 'native':
        if pos_encoding in ('rope','rpb','rope_rpb'):
            raise ValueError("encoder_impl='native' 不支持 RoPE/RPB。请改为 encoder_impl='custom' 或 pos_encoding ∈ {none, abs, sinus}.")
        if drop_path_rate and drop_path_rate > 0:
            raise ValueError("encoder_impl='native' 不支持 DropPath（drop_path_rate>0）。请设置 drop_path_rate=0 或改用 'custom'.")
        return 'native'

    if encoder_impl == 'custom':
        return 'custom'

    # auto
    if (pos_encoding in ('none','abs','sinus')) and (drop_path_rate == 0.0):
        return 'native'
    return 'custom'


