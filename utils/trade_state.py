import json
import os

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
state_file = os.path.join(project_root, 'trade_state.json')

def get_empty_state():
    """返回标准化的空状态字典"""
    return {
        "position_side": None,
        "position_amount": 0,
        "entry_price": 0.0,
        "active_sl": 0.0,
        "active_tp1": 0.0,
        "active_tp2": 0.0,
        "open_fee": 0.0,
        "margin_used": 0.0,
        "strategy_name": "",
        "signal_reason": "",
        "entry_time": "",
        "has_moved_to_breakeven": False,
        "has_taken_partial_profit": False,
        "exchange_order_ids": {
            "sl_order": None, 
            "tp_order": None
        }
    }

def load_state():
    """读取本地状态。如果没有文件或读取失败，返回空状态"""
    if not os.path.exists(state_file):
        return get_empty_state()
    try:
        with open(state_file, 'r', encoding='utf-8') as f:
            state = json.load(f)
            # 做一次简单的字段对齐，防止旧版 JSON 少了新字段
            empty = get_empty_state()
            for k in empty.keys():
                if k not in state:
                    state[k] = empty[k]
            return state
    except Exception as e:
        print(f"❌ 读取本地状态文件失败: {e}，将返回空状态。")
        return get_empty_state()

def save_state(state):
    """原子化写入本地状态，防崩溃损坏"""
    try:
        temp_file = state_file + ".tmp"
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=4, ensure_ascii=False)
        # 写入完成后瞬间替换原文件，绝对安全
        os.replace(temp_file, state_file)
    except Exception as e:
        print(f"❌ 保存本地状态文件失败: {e}")

def clear_state():
    """清空本地状态（平仓后调用）"""
    save_state(get_empty_state())