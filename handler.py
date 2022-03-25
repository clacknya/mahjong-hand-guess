import linecache
import random
import re
from collections import defaultdict, namedtuple
from enum import Enum

from mahjong.hand_calculating.hand import HandCalculator
from mahjong.hand_calculating.hand_config import HandConfig
from mahjong.tile import TilesConverter as TC
from nonebot import get_bot
from PIL import Image

from .imghandler import draw_text_by_line, easy_paste, get_font
from .mahjong_image import MahjongImage, TilebackType
from .utils import call_later, cancel_call_later, get_path, pil2b64
from .user import User


class TileAsciiMap(Enum):
    万 = "m"
    筒 = "p"
    索 = "s"
    东 = "1z"
    南 = "2z"
    西 = "3z"
    北 = "4z"
    白 = "5z"
    发 = "6z"
    中 = "7z"


TileMap = ["万", "筒", "索", "东", "南", "西", "北", "白", "发", "中"]


HandResult = namedtuple("HandResult", "tiles win_tile tsumo result hand_index")


async def get_hand(hand_index=None, **kwargs) -> HandResult:
    calculator = HandCalculator()

    hand_list = linecache.getlines(get_path("hands.txt"))  # 读取手牌列表
    hand_index = hand_index or random.randint(0, len(hand_list))  # 指定或者随机一组手牌
    hand_raw = hand_list[hand_index].strip()[:-3]

    tsumo = hand_raw[26] == "+"  # 是否为自摸
    last_tile = (hand_raw[26:28], hand_raw[27:29])[tsumo]  # 和牌

    tiles = TC.one_line_string_to_136_array(hand_raw.replace("+", ""))
    win_tile = TC.one_line_string_to_136_array(last_tile)[0]

    result = calculator.estimate_hand_value(
        tiles,
        win_tile,
        config=HandConfig(is_riichi=True, is_tsumo=tsumo),
        **kwargs,
    )

    tiles = TC.one_line_string_to_136_array(hand_raw[:26])

    return HandResult(tiles, last_tile, tsumo, result, hand_index)


UserState = namedtuple("UserState", "hit_count")
GroupState = namedtuple("GroupState", "start hand users")

HandGuessProcess = defaultdict(lambda: GroupState(False, None, {}))


class HandGuess:
    __slots__ = ["qq", "group"]

    MAX_GUESS = 6  # 每人最大猜测次数
    TIMEOUT = 5 * 60  # 一局结束超时时间

    def __init__(self, qq: int, group: int):
        self.qq = qq
        self.group = group

    @property
    def status(self) -> GroupState:
        return HandGuessProcess[self.group]

    def is_start(self):
        return self.status.start

    def reset_game(self):
        HandGuessProcess[self.group] = GroupState(False, None, {})

    async def timeout(self):
        await get_bot().send_group_msg(group_id=self.group, message="游戏已超时, 请重新开始")
        self.reset_game()

    async def start(self):
        if self.is_start():
            return dict(error=True, msg="当前游戏已经开始")

        # 生成手牌
        hand_res = await get_hand()
        HandGuessProcess[self.group] = GroupState(
            True, hand_res, defaultdict(lambda: UserState(0))
        )
        print(TC.to_one_line_string(hand_res.tiles) + hand_res.win_tile)
        call_later(self.TIMEOUT, self.timeout, "HandGuessGame")

        return dict(error=False)

    @staticmethod
    def format_hand_msg(msg: str):
        hand = ""
        for w in msg:
            if w in TileMap:
                hand += TileAsciiMap[w].value
            else:
                hand += w

        if hand[:-2][-1].isdigit():
            hand = hand[:-2] + hand[-1] + hand[-2:]
        return hand

    def inc_user_count(self):
        info = self.status.users[self.qq]
        count = info.hit_count + 1
        self.status.users[self.qq] = info._replace(hit_count=count)

    def is_win(self, tiles: list):
        wind_tile = TC.one_line_string_to_136_array(self.status.hand.win_tile)
        set_tiles = self.status.hand.tiles + wind_tile
        return set_tiles == tiles

    def win_game(self, points: int):
        self.reset_game()
        cancel_call_later("HandGuessGame")
        user = User(self.qq)
        user.add_points(points)
        return f"恭喜你, 猜对了, 积分增加 {points} 点, 当前积分 {user.points}"

    async def guesses_handler(self, msg: str):
        # pass不合法的信息
        if re.search(f"[^\dmpszh{''.join(TileMap)}]", msg):
            return dict(error=True, msg="")

        if self.status.users[self.qq].hit_count >= self.MAX_GUESS:
            return dict(error=True, msg="你已经没有次数了!")

        msg_hand = HandGuess.format_hand_msg(msg)
        msg_win_tile = msg_hand[-2:]

        msg_tiles = TC.one_line_string_to_136_array(msg_hand)
        if len(msg_tiles) != 14:
            return dict(error=True, msg="不是, 说好的14张牌呢")

        win_tile = TC.one_line_string_to_136_array(msg_win_tile)[0]
        calculator = HandCalculator()
        # 默认立直 , 是否自摸看生成的牌组
        result = calculator.estimate_hand_value(
            msg_tiles,
            win_tile,
            config=HandConfig(is_riichi=True, is_tsumo=self.status.hand.tsumo),
        )

        if result.han is None:
            return dict(error=True, msg="你这牌都没胡啊")
        if result.han == 0:
            return dict(error=True, msg="你无役了")

        current_tiles = TC.one_line_string_to_136_array(msg_hand[:-2])

        blue = MahjongImage(TilebackType.blue)
        orange = MahjongImage(TilebackType.orange)
        no_color = MahjongImage(TilebackType.no_color)

        # 手牌
        hand_img = Image.new("RGB", (80 * 13, 130), "#6c6c6c")
        for index, tile in enumerate(current_tiles):

            original = self.status.hand.tiles[index]
            ascii_tile = TC.to_one_line_string([tile])
            pos = (index * 80, 0)
            if tile == original:
                # 如果位置正确
                easy_paste(hand_img, blue.tile(ascii_tile), pos)
            elif tile in self.status.hand.tiles:
                # 如果存在
                easy_paste(hand_img, orange.tile(ascii_tile), pos)
            else:
                # 否则不存在
                easy_paste(hand_img, no_color.tile(ascii_tile), pos)

        # 胡牌
        wind_img = Image.new("RGB", (80, 130), "#6c6c6c")
        pos = (0, 0)
        ascii_tile = TC.to_one_line_string([win_tile])
        if msg_win_tile == self.status.hand.win_tile:
            easy_paste(wind_img, blue.tile(ascii_tile), pos)
        elif tile in self.status.hand.tiles:
            # 如果存在
            easy_paste(wind_img, orange.tile(ascii_tile), pos)
        else:
            # 否则不存在
            easy_paste(wind_img, no_color.tile(ascii_tile), pos)

        # 役提示
        yaku = [x for x in self.status.hand.result.yaku if x.yaku_id not in [0, 1]]
        yaku.reverse()
        tip = "提示: " + " ".join([x.japanese for x in yaku])

        # 番提示
        status_han = self.status.hand.result.han
        status_fu = self.status.hand.result.fu
        status_cost = (
            self.status.hand.result.cost["main"]
            + self.status.hand.result.cost["additional"]
        )
        tsumo_tip = ("", ",自摸")[self.status.hand.tsumo]
        han_tip = f"{status_han}番{status_fu}符 {status_cost}点 (包括立直{tsumo_tip})"

        background = Image.new("RGB", (1200, 400), "#EEEEEE")

        last = self.MAX_GUESS - self.status.users[self.qq].hit_count - 1
        draw_text_by_line(
            background, (26.5, 25), f"剩余{last}回", get_font(40), "#475463", 255
        )
        draw_text_by_line(
            background, (403.5, 25), tip, get_font(40), "#475463", 800, True
        )
        draw_text_by_line(
            background,
            (194.5, 122),
            han_tip,
            get_font(40),
            "#475463",
            1200,
            True,
        )

        easy_paste(background, hand_img.convert("RGBA"), (30, 226))
        easy_paste(background, wind_img.convert("RGBA"), (13 * 80 + 50, 226))

        ret_msg = ""
        if self.is_win(current_tiles + [win_tile]):
            ret_msg = self.win_game(status_cost)
        else:
            self.inc_user_count()

        return dict(error=False, img=pil2b64(background), msg=ret_msg)
