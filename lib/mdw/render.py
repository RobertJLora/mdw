#!/usr/bin/env python3
"""render.py: the terminal markdown renderer behind mdw.

Rendering strategy:
  The whole document goes through glow with a custom glamour style
  (share/mdw/styles/rose-pine-moon.json or rose-pine-dawn.json). glow wraps
  table cells to the target width, colours headings as text (no filled
  background blocks), and syntax-highlights fenced code via chroma. The output
  then gets a layout pass here: full-width rules, a heading hierarchy, hanging
  indents on list items, a task-list checklist, repaired blockquote wrapping,
  clickable OSC 8 links, and a hard fold so nothing overflows the pane.

  glow degrades truecolour to 16-colour ANSI when its stdout is a pipe, so it
  runs under a pseudo-terminal with COLORTERM=truecolor and emits exact 24-bit
  colours, independent of the terminal's own 16-colour palette.

Inline images (--images): a standalone `![alt](path)` line whose file resolves
(relative to the .md file) to a readable image is rendered as kitty graphics
protocol Unicode placeholder cells: U+10EEEE plus a row and a column diacritic
per cell, with the image id carried in the 24-bit foreground colour. The
placeholder rows are ordinary text lines, so the image scrolls, clips at the
top and bottom of the pane, and survives repaints exactly like text. This file
never writes graphics escapes itself: it lists each image (id, cols, rows, png
path) in the --images-out sidecar, and mdw, which owns the tty, transmits each
image once and creates its virtual placement. A missing or unreadable image
keeps the markdown line, so glow shows the alt text.

Usage:
  render.py --columns N [--style GLOW_STYLE_JSON] /abs/file.md
  render.py --columns N --images --pane-rows R --images-out SIDECAR /abs/file.md
    Cell pixel size: --cell-px WxH, else MDW_CELL_PX=WxH, else the tty's
    TIOCGWINSZ pixel fields, else 9x19.

Appearance: MDW_APPEARANCE=dark|light (mdw sets it), else MDW_THEME, else the
macOS appearance, else dark. Style: --style, else $MDW_GLOW_STYLE, else the
bundled Rose Pine style for the appearance.
Fallback chain (never leaves the viewer blank): glow under pty (truecolour) ->
glow via pipe (16-colour) -> mdcat --ansi if installed -> raw file text.
"""
import os
import pty
import re
import select
import subprocess
import sys
import termios
import time
import unicodedata


def _is_dark():
    """True when the terminal background is dark. mdw resolves this once and
    passes it down in MDW_APPEARANCE so the pager chrome and the render always
    agree; standalone runs fall back to MDW_THEME, then the macOS appearance."""
    for var in ('MDW_APPEARANCE', 'MDW_THEME'):
        v = os.environ.get(var, '').lower()
        if v in ('dark', 'light'):
            return v == 'dark'
    if sys.platform == 'darwin':
        try:
            r = subprocess.run(['defaults', 'read', '-g', 'AppleInterfaceStyle'],
                               capture_output=True, text=True, timeout=2)
            return r.returncode == 0 and 'Dark' in r.stdout
        except Exception:
            pass
    return True


DARK = _is_dark()
STYLE_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', '..', 'share', 'mdw', 'styles'))
DEFAULT_STYLE = os.path.join(
    STYLE_DIR, 'rose-pine-moon.json' if DARK else 'rose-pine-dawn.json')

# ---------------------------------------------------------------------------
# Palette: one role table, Rose Pine Dawn on a light background and Rose Pine
# on a dark one. Every colour this file paints itself (task glyphs, rules, list
# markers, quote rails, muted asides) is named here once, so the two
# appearances cannot drift apart and no hex is written twice.
#
#   accent  the "still open" colour, the loudest thing on the page
#   done    resolved, present but finished
#   label   a short meta tag inside prose (a status word, never a sentence)
#   quiet   secondary prose: readable, clearly not the point
#   rule    structure (rules, bullets, quote rails) that must never outshout text
#   alert   a render failure the reader must not miss
#
# Colour belongs to text. Every rule this file draws is neutral and carries its
# level in weight instead (a heavy rule under a title, a light one over a
# section), so a full-width line never outshouts the heading it serves.
#
# `done` is pine in both appearances. On a dark ground foam and the open-task
# gold read at nearly the same weight, so finished and pending items looked
# alike; pine sits level with the muted text of the item it marks, so a done
# item recedes. Dark uses Moon's brighter pine #3e8fb0 rather than Main's
# #31748f, which is hard to read on near-black. Dawn uses bronze #b7834c for
# the accent, the readable gold-on-parchment blend.
if DARK:      # Rose Pine, on the Main base #191724
    C_ACCENT, C_DONE, C_LABEL = '#f6c177', '#3e8fb0', '#c4a7e7'
    C_QUIET, C_RULE = '#908caa', '#6e6a86'
    C_ALERT = '#eb6f92'
else:         # Rose Pine Dawn, on the Dawn base #faf4ed
    C_ACCENT, C_DONE, C_LABEL = '#b7834c', '#286983', '#907aa9'
    C_QUIET, C_RULE = '#797593', '#9893a5'
    C_ALERT = '#b4637a'


def _sgr(hexcolor, bold=False):
    """Truecolour SGR sequence for a #rrggbb value."""
    r, g, b = (int(hexcolor[i:i + 2], 16) for i in (1, 3, 5))
    return '\x1b[%s38;2;%d;%d;%dm' % ('1;' if bold else '', r, g, b)


SGR_OFF = '\x1b[0m'
SGR_ACCENT = _sgr(C_ACCENT, bold=True)
SGR_DONE = _sgr(C_DONE)
SGR_LABEL = _sgr(C_LABEL, bold=True)
SGR_QUIET = _sgr(C_QUIET)
# Done task items are struck through as well as muted, so a finished item stays
# visible on the list but reads as closed.
SGR_STRIKE = '\x1b[9m'
SGR_RULE = _sgr(C_RULE)
# Numbered heading prefix ("2.") is rose: distinct from link foam, done pine,
# label iris and accent gold, so a numbered heading cannot read as a link.
SGR_HNUM = _sgr('#ebbcba') if DARK else _sgr('#d7827e')

# Loud-fallback logging. A silent raw-markdown fallback (glow/mdcat all fail and
# we dump the source) must never masquerade as a styling choice, so every stage
# failure is logged and the raw path prints a visible banner.
LOG_PATH = os.path.join(os.environ.get('XDG_STATE_HOME') or os.path.expanduser('~/.local/state'),
                        'mdw', 'render.log')
# The banner follows the appearance so it stays readable on either ground.
RAW_BANNER = (
    _sgr(C_ALERT) + '[mdw: styled render failed, showing raw markdown; '
    'see ' + LOG_PATH.replace(os.path.expanduser('~'), '~') + ']' + SGR_OFF + '\n'
)


def _log(msg):
    try:
        import datetime
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, 'a', encoding='utf-8') as fh:
            fh.write('%s %s\n' % (datetime.datetime.now().isoformat(timespec='seconds'), msg))
    except Exception:
        pass


def _emit_raw(path):
    """Last-resort raw dump with a visible banner (only when there is real content
    to show, so a legitimately empty file does not trigger a false alarm)."""
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            raw = fh.read()
    except Exception as e:
        _log('raw read failed for %s: %r' % (path, e))
        return
    sys.stdout.write((RAW_BANNER + raw) if raw.strip() else raw)

# Cap the source size we feed to glow. glow is CPU-bound (~4.4s per MB), so a
# huge file would freeze this live viewer on every repaint. Above this we render
# a truncated head plus a visible notice, keeping repaints to a few seconds.
# Typical notes are tens of KB; 1 MB covers them many times over.
MAX_BYTES = int(os.environ.get('MDW_MAX_BYTES') or 1048576)

# Hard backstop against a genuine glow hang (should never trip now that we answer
# its terminal queries, but keeps the viewer from freezing forever if glow wedges
# on something unforeseen). Generous enough that a full 1 MB render completes.
PTY_TIMEOUT = float(os.environ.get('MDW_PTY_TIMEOUT') or 20)

# The pipe fallbacks need their own deadline. glow in pipe mode still writes its
# terminal-detection queries and waits for answers that a pipe can never send,
# so a run_pipe call with no timeout can sit there forever (measured: a forced
# fallback held the viewer for over 120s before it was killed). A few seconds is
# far more than either renderer needs on ordinary documents, and a stage that
# overruns it is a stage that has wedged, so the chain should move on to the
# next renderer rather than freeze the pane.
PIPE_TIMEOUT = float(os.environ.get('MDW_PIPE_TIMEOUT') or 5)

# Terminal-detection queries a renderer emits when it thinks it is on a real
# terminal: OSC 10/11/12 colour queries, the CSI DSR request (CSI 5n / 6n), the
# CSI R cursor report, and Device Attributes (CSI c / CSI > c / CSI = c /
# CSI ? ... c). glow writes these into the pty, so they land in our captured
# output even though we answer them; left in, writing them to the real pane makes
# the terminal reply into mdw's key loop (the stray "^[[?62;22;52c"). Strip every
# one. None end in `m`, so SGR colour codes are never touched.
TERM_QUERY = re.compile(
    r'\x1b\]1[012];[^\x07\x1b]*(?:\x07|\x1b\\)'
    r'|\x1b\[[0-9;]*[56]n'
    r'|\x1b\[[0-9;]*R'
    r'|\x1b\[[?>=]?[0-9;]*c'
)

# Split off SGR colour/style codes to measure the visible width of a line.
SGR = re.compile(r'\x1b\[[0-9;]*m')


# ---------------------------------------------------------------------------
# Emoji: shortcodes to glyphs, and terminal display width
# ---------------------------------------------------------------------------
# Slack and GitHub write emoji as shortcodes (`:ok_hand:`, `:white_check_mark:`)
# and Slack appends a skin tone as its own code (`:ok_hand::skin-tone-4:`).
# glow prints them as literal text, so convert_shortcodes() swaps each known
# code for its glyph BEFORE glow renders, in the temp copy only: the file on
# disk keeps the raw text. Fenced code and inline code spans stay literal.
# Unknown codes (custom Slack emoji, `:foo:` that is just prose) stay as typed.
# Lookup order: the map below, then the Unicode character name
# (`:rocket:` -> ROCKET), accepted only when it names a wide (emoji) glyph.
# Disable: MDW_EMOJI=0.
EMOJI_ENABLED = os.environ.get('MDW_EMOJI', '1') != '0'

_EMOJI_PAIRS = '''
+1 👍|thumbsup 👍|-1 👎|thumbsdown 👎|ok_hand 👌|wave 👋|clap 👏|raised_hands 🙌
pray 🙏|muscle 💪|point_right 👉|point_left 👈|point_up ☝️|point_up_2 👆
point_down 👇|v ✌️|crossed_fingers 🤞|fist 👊|facepunch 👊|punch 👊|raised_hand ✋
hand ✋|hand_raised ✋|writing_hand ✍️|handshake 🤝|call_me_hand 🤙|pinched_fingers 🤌
metal 🤘|the_horns 🤘|open_hands 👐|palms_up_together 🤲|eyes 👀|eye 👁️|brain 🧠
white_check_mark ✅|heavy_check_mark ✔️|check ✔️|ballot_box_with_check ☑️|x ❌
heavy_multiplication_x ✖️|negative_squared_cross_mark ❎|warning ⚠️|no_entry ⛔
no_entry_sign 🚫|stop_sign 🛑|question ❓|grey_question ❔|exclamation ❗
heavy_exclamation_mark ❗|grey_exclamation ❕|bangbang ‼️|interrobang ⁉️
heavy_plus_sign ➕|heavy_minus_sign ➖|arrow_right ➡️|arrow_left ⬅️|arrow_up ⬆️
arrow_down ⬇️|arrow_upper_right ↗️|arrow_lower_right ↘️|arrows_counterclockwise 🔄
repeat 🔁|leftwards_arrow_with_hook ↩️|arrow_right_hook ↪️|arrow_forward ▶️
fast_forward ⏩|rewind ⏪|pause_button ⏸️|red_circle 🔴|large_blue_circle 🔵
blue_circle 🔵|large_green_circle 🟢|green_circle 🟢|large_yellow_circle 🟡
yellow_circle 🟡|large_orange_circle 🟠|orange_circle 🟠|large_purple_circle 🟣
purple_circle 🟣|white_circle ⚪|black_circle ⚫|red_square 🟥|green_square 🟩
yellow_square 🟨|large_blue_square 🟦|blue_square 🟦|white_large_square ⬜
black_large_square ⬛|small_red_triangle 🔺|small_red_triangle_down 🔻
large_orange_diamond 🔶|large_blue_diamond 🔷|small_orange_diamond 🔸
small_blue_diamond 🔹|heavy_dollar_sign 💲|100 💯|1234 🔢|hash #️⃣|zero 0️⃣|one 1️⃣
two 2️⃣|three 3️⃣|four 4️⃣|five 5️⃣|six 6️⃣|seven 7️⃣|eight 8️⃣|nine 9️⃣|keycap_ten 🔟
new 🆕|free 🆓|up 🆙|cool 🆒|ok 🆗|sos 🆘|top 🔝|soon 🔜|end 🔚|back 🔙|on 🔛
information_source ℹ️|copyright ©️|registered ®️|tm ™️|recycle ♻️|infinity ♾️
heart ❤️|red_heart ❤️|orange_heart 🧡|yellow_heart 💛|green_heart 💚|blue_heart 💙
purple_heart 💜|black_heart 🖤|white_heart 🤍|brown_heart 🤎|broken_heart 💔
two_hearts 💕|sparkling_heart 💖|heartpulse 💗|heartbeat 💓|revolving_hearts 💞
heart_on_fire ❤️‍🔥|smile 😄|smiley 😃|grinning 😀|grin 😁|laughing 😆|satisfied 😆
sweat_smile 😅|joy 😂|rofl 🤣|rolling_on_the_floor_laughing 🤣|slightly_smiling_face 🙂
upside_down_face 🙃|wink 😉|blush 😊|innocent 😇|smiling_face_with_3_hearts 🥰
heart_eyes 😍|star_struck 🤩|kissing_heart 😘|yum 😋|stuck_out_tongue 😛
stuck_out_tongue_winking_eye 😜|zany_face 🤪|money_mouth_face 🤑|hugging_face 🤗
hugs 🤗|hand_over_mouth 🤭|shushing_face 🤫|thinking 🤔|thinking_face 🤔
zipper_mouth_face 🤐|raised_eyebrow 🤨|neutral_face 😐|expressionless 😑
no_mouth 😶|smirk 😏|unamused 😒|roll_eyes 🙄|face_with_rolling_eyes 🙄
grimacing 😬|lying_face 🤥|relieved 😌|pensive 😔|sleepy 😪|sleeping 😴
mask 😷|face_with_thermometer 🤒|nauseated_face 🤢|sneezing_face 🤧|hot_face 🥵
cold_face 🥶|woozy_face 🥴|dizzy_face 😵|exploding_head 🤯|mind_blown 🤯
cowboy_hat_face 🤠|partying_face 🥳|sunglasses 😎|nerd_face 🤓|monocle_face 🧐
confused 😕|worried 😟|slightly_frowning_face 🙁|frowning_face ☹️|open_mouth 😮
hushed 😯|astonished 😲|flushed 😳|pleading_face 🥺|frowning 😦|anguished 😧
fearful 😨|cold_sweat 😰|disappointed_relieved 😥|cry 😢|sob 😭|scream 😱
confounded 😖|persevere 😣|disappointed 😞|sweat 😓|weary 😩|tired_face 😫
yawning_face 🥱|triumph 😤|rage 😡|pout 😡|angry 😠|cursing_face 🤬|smiling_imp 😈
imp 👿|skull 💀|skull_and_crossbones ☠️|poop 💩|hankey 💩|clown_face 🤡
ghost 👻|alien 👽|robot_face 🤖|robot 🤖|see_no_evil 🙈|hear_no_evil 🙉
speak_no_evil 🙊|saluting_face 🫡|melting_face 🫠|face_holding_back_tears 🥹
dotted_line_face 🫥|shrug 🤷|man_shrugging 🤷‍♂️|woman_shrugging 🤷‍♀️
facepalm 🤦|face_palm 🤦|man_facepalming 🤦‍♂️|woman_facepalming 🤦‍♀️
bow 🙇|raising_hand 🙋|ok_woman 🙆|no_good 🙅|information_desk_person 💁
tipping_hand_person 💁|person_tipping_hand 💁|man_technologist 👨‍💻
woman_technologist 👩‍💻|technologist 🧑‍💻|family 👪|runner 🏃|running 🏃|walking 🚶
dancer 💃|man_dancing 🕺|tada 🎉|confetti_ball 🎊|balloon 🎈|gift 🎁|trophy 🏆
medal 🏅|sports_medal 🏅|first_place_medal 🥇|second_place_medal 🥈
third_place_medal 🥉|dart 🎯|direct_hit 🎯|crown 👑|gem 💎|star ⭐|star2 🌟
glowing_star 🌟|sparkles ✨|dizzy 💫|boom 💥|collision 💥|fire 🔥|zap ⚡|high_voltage ⚡
rocket 🚀|rainbow 🌈|sunny ☀️|cloud ☁️|umbrella ☔|snowflake ❄️|droplet 💧
ocean 🌊|earth_americas 🌎|earth_africa 🌍|earth_asia 🌏|globe_with_meridians 🌐
seedling 🌱|herb 🌿|four_leaf_clover 🍀|evergreen_tree 🌲|deciduous_tree 🌳
palm_tree 🌴|cactus 🌵|sunflower 🌻|rose 🌹|tulip 🌷|cherry_blossom 🌸
bouquet 💐|fallen_leaf 🍂|maple_leaf 🍁|mushroom 🍄|dog 🐶|cat 🐱|mouse 🐭
rabbit 🐰|fox_face 🦊|bear 🐻|panda_face 🐼|koala 🐨|tiger 🐯|lion 🦁|lion_face 🦁
cow 🐮|pig 🐷|frog 🐸|monkey_face 🐵|monkey 🐒|chicken 🐔|penguin 🐧|bird 🐦
eagle 🦅|owl 🦉|unicorn 🦄|unicorn_face 🦄|bee 🐝|honeybee 🐝|bug 🐛|butterfly 🦋
snail 🐌|turtle 🐢|snake 🐍|octopus 🐙|crab 🦀|whale 🐳|dolphin 🐬|fish 🐟
shark 🦈|goat 🐐|sloth 🦥|coffee ☕|tea 🍵|beer 🍺|beers 🍻|wine_glass 🍷
cocktail 🍸|champagne 🍾|clinking_glasses 🥂|pizza 🍕|hamburger 🍔|fries 🍟
taco 🌮|burrito 🌯|sushi 🍣|ramen 🍜|bread 🍞|croissant 🥐|cake 🎂
birthday 🎂|cookie 🍪|doughnut 🍩|chocolate_bar 🍫|popcorn 🍿|apple 🍎
green_apple 🍏|banana 🍌|avocado 🥑|lemon 🍋|strawberry 🍓|watermelon 🍉
peach 🍑|eggplant 🍆|hot_pepper 🌶️|house 🏠|office 🏢|hospital 🏥|bank 🏦
school 🏫|car 🚗|red_car 🚗|taxi 🚕|bus 🚌|train 🚆|airplane ✈️|ship 🚢
bike 🚲|bicyclist 🚴|construction 🚧|rotating_light 🚨|traffic_light 🚥
vertical_traffic_light 🚦|world_map 🗺️|compass 🧭|round_pushpin 📍|pushpin 📌
paperclip 📎|link 🔗|lock 🔒|unlock 🔓|key 🔑|old_key 🗝️|hammer 🔨|wrench 🔧
hammer_and_wrench 🛠️|gear ⚙️|nut_and_bolt 🔩|toolbox 🧰|magnet 🧲|bulb 💡
light_bulb 💡|flashlight 🔦|battery 🔋|electric_plug 🔌|computer 💻|laptop 💻
desktop_computer 🖥️|keyboard ⌨️|iphone 📱|mobile_phone 📱|phone ☎️|telephone ☎️
telephone_receiver 📞|calling 📲|email 📧|e-mail 📧|envelope ✉️|incoming_envelope 📨
envelope_with_arrow 📩|mailbox 📫|inbox_tray 📥|outbox_tray 📤|package 📦
memo 📝|pencil 📝|pencil2 ✏️|pen 🖊️|black_nib ✒️|crayon 🖍️|paintbrush 🖌️
clipboard 📋|calendar 📆|date 📅|spiral_calendar 🗓️|card_index 📇
chart_with_upwards_trend 📈|chart_with_downwards_trend 📉|bar_chart 📊
file_folder 📁|open_file_folder 📂|page_facing_up 📄|page_with_curl 📃
bookmark_tabs 📑|books 📚|book 📖|open_book 📖|notebook 📓|ledger 📒
closed_book 📕|green_book 📗|blue_book 📘|orange_book 📙|bookmark 🔖|label 🏷️
newspaper 📰|scroll 📜|mag 🔍|mag_right 🔎|microscope 🔬|telescope 🔭
satellite 📡|loudspeaker 📢|mega 📣|bell 🔔|no_bell 🔕|speech_balloon 💬
thought_balloon 💭|left_speech_bubble 🗨️|speaking_head 🗣️|busts_in_silhouette 👥
bust_in_silhouette 👤|moneybag 💰|money_with_wings 💸|dollar 💵|euro 💶
pound 💷|yen 💴|credit_card 💳|receipt 🧾|chart 💹|shopping_cart 🛒
shopping_bags 🛍️|hourglass ⌛|hourglass_flowing_sand ⏳|stopwatch ⏱️|timer_clock ⏲️
alarm_clock ⏰|watch ⌚|clock1 🕐|clock3 🕒|clock12 🕛|mantelpiece_clock 🕰️
camera 📷|camera_flash 📸|movie_camera 🎥|video_camera 📹|tv 📺|film_frames 🎞️
clapper 🎬|headphones 🎧|microphone 🎤|studio_microphone 🎙️|musical_note 🎵
notes 🎶|guitar 🎸|art 🎨|video_game 🎮|joystick 🕹️|game_die 🎲|jigsaw 🧩
puzzle_piece 🧩|chess_pawn ♟️|soccer ⚽|basketball 🏀|football 🏈|tennis 🎾
golf ⛳|checkered_flag 🏁|triangular_flag_on_post 🚩|white_flag 🏳️
black_flag 🏴|rainbow_flag 🏳️‍🌈|pirate_flag 🏴‍☠️|flag-us 🇺🇸|us 🇺🇸|flag-es 🇪🇸
es 🇪🇸|flag-gb 🇬🇧|gb 🇬🇧|uk 🇬🇧|flag-pl 🇵🇱|flag-mx 🇲🇽|flag-ca 🇨🇦|flag-de 🇩🇪
de 🇩🇪|flag-fr 🇫🇷|fr 🇫🇷|flag-it 🇮🇹|it 🇮🇹|flag-pt 🇵🇹|flag-br 🇧🇷|flag-au 🇦🇺
flag-in 🇮🇳|flag-jp 🇯🇵|jp 🇯🇵|flag-cn 🇨🇳|cn 🇨🇳|flag-kr 🇰🇷|kr 🇰🇷|flag-eu 🇪🇺
flag-nl 🇳🇱|flag-ie 🇮🇪|flag-ar 🇦🇷|flag-co 🇨🇴|flag-ua 🇺🇦|flag-il 🇮🇱
lipstick 💄|ring 💍|shirt 👕|tshirt 👕|jeans 👖|necktie 👔|dress 👗|briefcase 💼
handbag 👜|school_satchel 🎒|backpack 🎒|luggage 🧳|eyeglasses 👓
dark_sunglasses 🕶️|tophat 🎩|mortar_board 🎓|graduation_cap 🎓|syringe 💉
pill 💊|dna 🧬|test_tube 🧪|petri_dish 🧫|abacus 🧮|bricks 🧱|chains ⛓️
shield 🛡️|crossed_swords ⚔️|bomb 💣|knife 🔪|dagger 🗡️|gun 🔫|bow_and_arrow 🏹
crystal_ball 🔮|nazar_amulet 🧿|moyai 🗿|placard 🪧|mirror 🪞|window 🪟
bed 🛏️|couch_and_lamp 🛋️|door 🚪|toilet 🚽|shower 🚿|bathtub 🛁|broom 🧹
basket 🧺|soap 🧼|sponge 🧽|fire_extinguisher 🧯|candle 🕯️|wastebasket 🗑️
ballot_box 🗳️|file_cabinet 🗄️|card_file_box 🗃️|scissors ✂️|straight_ruler 📏
triangular_ruler 📐|lock_with_ink_pen 🔏|closed_lock_with_key 🔐|new_moon 🌑
full_moon 🌕|crescent_moon 🌙|sun_with_face 🌞|mostly_sunny 🌤️|partly_sunny ⛅
thunder_cloud_and_rain ⛈️|tornado 🌪️|fog 🌫️|snowman ⛄|comet ☄️|volcano 🌋
mountain ⛰️|snow_capped_mountain 🏔️|beach_with_umbrella 🏖️|desert 🏜️
national_park 🏞️|camping 🏕️|tent ⛺|sunrise 🌅|city_sunset 🌆|night_with_stars 🌃
milky_way 🌌|bridge_at_night 🌉|stadium 🏟️|statue_of_liberty 🗽|tokyo_tower 🗼
ferris_wheel 🎡|roller_coaster 🎢|circus_tent 🎪|performing_arts 🎭|ticket 🎫
admission_tickets 🎟️|money_bag 💰|chart_increasing 📈|chart_decreasing 📉
large_blue_square 🟦|white_square_button 🔳|black_square_button 🔲|radio_button 🔘
white_small_square ▫️|black_small_square ▪️|small_blue_square 🔹|baby 👶
boy 👦|girl 👧|man 👨|woman 👩|older_man 👴|older_woman 👵|person 🧑|adult 🧑
detective 🕵️|cop 👮|police_officer 👮|construction_worker 👷|guard 💂|ninja 🥷
santa 🎅|superhero 🦸|zombie 🧟|genie 🧞|mermaid 🧜‍♀️|fairy 🧚|angel 👼
man_in_tuxedo 🤵|bride_with_veil 👰|pregnant_woman 🤰|couple 👫|two_men_holding_hands 👬
two_women_holding_hands 👭|kiss 💏|couple_with_heart 💑|footprints 👣|lips 👄
tongue 👅|ear 👂|nose 👃|bone 🦴|tooth 🦷|leg 🦵|foot 🦶|selfie 🤳|nail_care 💅
zzz 💤|sweat_drops 💦|dash 💨|anger 💢|hole 🕳️|speech_left 🗨️|love_letter 💌
cupid 💘|gift_heart 💝|heart_decoration 💟|kiss_mark 💋|ribbon 🎀|jack_o_lantern 🎃
christmas_tree 🎄|fireworks 🎆|sparkler 🎇|firecracker 🧨|tanabata_tree 🎋
bamboo 🎍|dolls 🎎|flags 🎏|wind_chime 🎐|rice_scene 🎑|red_envelope 🧧
magic_wand 🪄|pinata 🪅|nesting_dolls 🪆|thread 🧵|yarn 🧶|knot 🪢
'''

EMOJI_MAP = {}
for _pair in _EMOJI_PAIRS.replace('\n', '|').split('|'):
    _pair = _pair.strip()
    if _pair:
        _k, _v = _pair.split(' ', 1)
        EMOJI_MAP[_k] = _v
del _pair, _k, _v

# Slack's :skin-tone-N: (1 = default yellow, so no modifier) and GitHub's
# _toneN suffix both map onto the five Fitzpatrick modifiers U+1F3FB..1F3FF.
_SKIN_SLACK = {'1': '', '2': '\U0001F3FB', '3': '\U0001F3FC', '4': '\U0001F3FD',
               '5': '\U0001F3FE', '6': '\U0001F3FF'}
_SKIN_TONE = {'1': '\U0001F3FB', '2': '\U0001F3FC', '3': '\U0001F3FD',
              '4': '\U0001F3FE', '5': '\U0001F3FF'}
_SHORTCODE = re.compile(
    r'(?<![\w/]):([a-z0-9+\-][a-z0-9_+\-]*?)(?:_tone([1-5]))?:(?::skin-tone-([1-6]):)?')
_CODE_SPAN = re.compile(r'(`+).*?\1')


def _emoji_for(name):
    if name in EMOJI_MAP:
        return EMOJI_MAP[name]
    if name.startswith('skin-tone-') and name[10:] in _SKIN_SLACK:
        return _SKIN_SLACK[name[10:]]
    try:
        ch = unicodedata.lookup(name.replace('_', ' ').replace('-', ' ').upper())
    except KeyError:
        return None
    return ch if unicodedata.east_asian_width(ch) == 'W' else None


def _with_tone(glyph, tone):
    """Attach a skin-tone modifier to the first code point of `glyph` (dropping
    the U+FE0F that would sit between them), so `✌️` + tone is one glyph."""
    if not tone:
        return glyph
    rest = glyph[1:]
    if rest.startswith('️'):
        rest = rest[1:]
    return glyph[0] + tone + rest


def _sub_shortcodes(s):
    def repl(m):
        name, gh_tone, slack_tone = m.group(1), m.group(2), m.group(3)
        glyph = _emoji_for(name)
        if glyph is None and gh_tone:
            glyph = _emoji_for(name + '_tone' + gh_tone)
            gh_tone = None
        if glyph is None:
            return m.group(0)
        tone = _SKIN_TONE[gh_tone] if gh_tone else _SKIN_SLACK.get(slack_tone or '1', '')
        return _with_tone(glyph, tone)
    return _SHORTCODE.sub(repl, s)


def convert_shortcodes(text):
    """Replace emoji shortcodes outside code with glyphs. Returns
    (new_text, changed)."""
    if ':' not in text:
        return text, False
    out = []
    fence = None
    changed = False
    for ln in text.split('\n'):
        f = _FENCE.match(ln)
        if f:
            if fence is None:
                fence = f.group(1)[0] * 3
            elif ln.lstrip().startswith(fence):
                fence = None
            out.append(ln)
            continue
        if fence is not None or ln.count(':') < 2:
            out.append(ln)
            continue
        pieces = []
        last = 0
        for m in _CODE_SPAN.finditer(ln):
            pieces.append(_sub_shortcodes(ln[last:m.start()]))
            pieces.append(m.group(0))
            last = m.end()
        pieces.append(_sub_shortcodes(ln[last:]))
        new = ''.join(pieces)
        changed = changed or new != ln
        out.append(new)
    return '\n'.join(out), changed


# Display width. A terminal cell count is not a code point count: an emoji is
# two cells, and a ZWJ family, a flag pair, a keycap or a skin-toned hand is
# several code points drawn as ONE glyph. Measuring with len() pushed emoji
# lines past the pane and cut clusters in half at a fold. Clusters here are an
# approximation of Unicode grapheme segmentation that covers emoji: a base code
# point plus any combining marks, variation selectors, skin-tone modifiers, tag
# characters, keycap marks, and anything joined by U+200D. Widths follow what
# Ghostty (grapheme-width-method = unicode) and glamour's own tables use: East
# Asian Wide/Fullwidth or VS16 presentation = 2, a regional-indicator pair = 2,
# VS15 text presentation = 1, format and combining characters alone = 0.
def _is_extender(ch):
    o = ord(ch)
    if o < 0x300:
        return False
    return (o == 0x200D or 0xFE00 <= o <= 0xFE0F or 0x1F3FB <= o <= 0x1F3FF
            or 0xE0020 <= o <= 0xE007F or o == 0x20E3
            or unicodedata.category(ch) in ('Mn', 'Me'))


def _is_ri(ch):
    return 0x1F1E6 <= ord(ch) <= 0x1F1FF


def clusters(s):
    """Split plain (SGR-free) text into display clusters."""
    out = []
    for ch in s:
        if out and (_is_extender(ch) or out[-1][-1] == '‍'
                    or (_is_ri(ch) and len(out[-1]) == 1 and _is_ri(out[-1]))):
            out[-1] += ch
        else:
            out.append(ch)
    return out


def cluster_width(c):
    base = c[0]
    o = ord(base)
    if o < 0x300 and len(c) == 1:
        return 0 if o < 0x20 or o == 0x7F else 1
    if _is_ri(base):
        return 2 if len(c) > 1 and _is_ri(c[1]) else 1
    if '︎' in c:
        return 1
    if unicodedata.east_asian_width(base) in ('W', 'F'):
        return 2
    if '️' in c or any(0x1F3FB <= ord(x) <= 0x1F3FF for x in c[1:]):
        return 2
    if len(c) == 1 and unicodedata.category(base) in ('Mn', 'Me', 'Cf'):
        return 0
    return 1


def text_width(s):
    """Terminal cell width of plain (SGR-free) text."""
    if s.isascii():
        return len(s)
    return sum(cluster_width(c) for c in clusters(s))


def vis_width(s):
    return text_width(SGR.sub('', s))


# Leading decoration of a wrapped line that must repeat on continuation lines:
# a blockquote gutter (│, optionally nested) and/or indentation. Matched on the
# ANSI-stripped text; the same visible width is then peeled off the raw line so
# the coloured gutter itself is carried onto each continuation.
_LEAD_DECOR = re.compile(r'^[ \t]*(?:│[ \t]*)+|^[ \t]+')


def _peel_visible(line, want):
    """Index just past the first `want` VISIBLE characters of `line`, with any
    SGR codes that sit inside (or immediately after) that run absorbed, so
    line[:index] re-emits the same coloured prefix and line[index:] starts on
    clean text."""
    seen = 0
    i = 0
    n = len(line)
    while i < n and seen < want:
        sm = SGR.match(line, i)
        if sm:
            i = sm.end()
        else:
            i += 1
            seen += 1
    sm = SGR.match(line, i)
    while sm:
        i = sm.end()
        sm = SGR.match(line, i)
    return i


def _split_leading(line):
    """Return (raw_prefix, prefix_visible_width) for a line's blockquote/indent
    decoration, or ('', 0) if it has none. raw_prefix includes the SGR codes that
    colour the gutter, so re-emitting it reproduces the gutter on a continuation."""
    vis = SGR.sub('', line)
    m = _LEAD_DECOR.match(vis)
    if not m:
        return ('', 0)
    return (line[:_peel_visible(line, len(m.group()))], vis_width(m.group()))


def fold_line(line, width):
    """Fold one rendered line to `width`, carrying its leading blockquote/indent
    decoration onto every continuation. Without this a folded blockquote line
    would drop its `│` gutter and the spill word would land flush-left at column
    0 (the reported orphan). Prose with no decoration folds exactly as before."""
    if width <= 0 or vis_width(line) <= width:
        return [line]
    prefix, pw = _split_leading(line)
    if prefix and pw < width - 8:
        body = line[len(prefix):]
        return [prefix + piece for piece in _fold_core(body, width - pw)]
    return _fold_core(line, width)


def _fold_core(line, width):
    """Hard-fold one rendered line so its visible width never exceeds `width`.
    glamour's word-wrap occasionally rounds a column or two past the target on
    list items, and it cannot break spaceless tokens (long paths/URLs) at all,
    so the terminal would otherwise hard-wrap them mid-character at a width wider
    than the pane. Break at the last space when there is one, else mid-token, and
    carry the active SGR colour state onto each continuation line so colours
    survive the fold. Table rows sit exactly at `width`, so they never fold."""
    if width <= 0 or vis_width(line) <= width:
        return [line]
    parts = []
    last = 0
    # Text is split into display clusters (an emoji with its skin tone or ZWJ
    # partners stays one piece) so a fold never cuts a glyph in half.
    for m in SGR.finditer(line):
        parts.extend(('t', c) for c in clusters(line[last:m.start()]))
        parts.append(('e', m.group()))
        last = m.end()
    parts.extend(('t', c) for c in clusters(line[last:]))

    result = []
    cur = []
    curw = 0
    active = []          # SGR codes in effect since the last reset
    break_at = -1        # index in `cur` where a continuation may start
    soft_at = -1         # last '-' or '/' boundary, used only when there is no space

    def printable_count(pieces):
        return sum(cluster_width(k) for k in pieces if not k.startswith('\x1b'))

    for kind, val in parts:
        if kind == 'e':
            cur.append(val)
            if val in ('\x1b[0m', '\x1b[m'):
                active = []
            else:
                active.append(val)
            continue
        w = cluster_width(val)
        if curw + w > width and curw > 0:
            # A space is always the best break. Failing that, a token too long
            # for the pane (a path, a URL) breaks after its last '-' or '/'
            # rather than mid-word, provided that lands past the halfway mark.
            at = break_at
            if at <= 0 and soft_at > 0 and printable_count(cur[:soft_at]) >= width // 2:
                at = soft_at
            if at > 0:
                head, tail = cur[:at], cur[at:]
                result.append(''.join(head) + ('\x1b[0m' if active else ''))
                cur = list(active) + tail
                curw = printable_count(tail)
            else:
                result.append(''.join(cur) + ('\x1b[0m' if active else ''))
                cur = list(active)
                curw = 0
            break_at = soft_at = -1
        cur.append(val)
        curw += w
        if val == ' ':
            break_at = len(cur)
        elif val in '-/':
            soft_at = len(cur)
    # Drop a final segment with no visible ink (only SGR codes / spaces): when
    # the text ends exactly at a fold boundary the leftover is empty, and
    # emitting it produced a phantom blank row; inside blockquotes fold_line
    # then glued the gutter onto it, showing a bare `│` line mid-quote.
    if any(not c.startswith('\x1b') and c != ' ' for c in cur) or not result:
        result.append(''.join(cur))
    return result


def _has_gutter(line):
    """True only for a blockquote gutter line. glow emits quote gutters at
    column 0 (`│ text`, nested `│ │ text`, bare `│`). Table continuation rows
    also reach here looking gutter-ish (a row whose first cell is empty renders
    as indent + `│`), and treating them as quotes let rewrap_quote_runs join and
    re-wrap wrapped table cells, shredding the column alignment. Two
    discriminators, either sufficient: tables are indented
    (glow always pads the row with leading space), and tables carry more `│`
    column separators after the leading run."""
    s = SGR.sub('', line)
    if not s.startswith('│'):
        return False
    body = _LEAD_DECOR.sub('', s, count=1)
    return '│' not in body


# A table continuation row whose every cell is blank: only `│` separators and
# whitespace, at least two separators (a bare quote gutter is a single `│`).
# glamour occasionally over-counts a row's cell height and pads the row with a
# run of these all-blank lines (nine phantom rows on one wide table seen);
# dropping them can never lose content.
_BLANK_TABLE_ROW = re.compile(r'^[ \t]*(?:│[ \t]*){2,}$')


def drop_blank_table_rows(text):
    return '\n'.join(
        ln for ln in text.split('\n')
        if not _BLANK_TABLE_ROW.match(SGR.sub('', ln))
    )


# ---------------------------------------------------------------------------
# Page furniture: rules, heading hierarchy, quote rails
# ---------------------------------------------------------------------------
# The glamour styles cannot know how wide the pane is, so anything that has to
# span it is emitted there as a one-character marker and drawn here instead.
# Heading levels arrive the same way: each hN style prefixes its own private-use
# marker (all inside the range _PUA_STRAY sweeps, so a marker that somehow
# survives can never reach the pane as tofu), which is what lets this file tell
# an H2 from an H4 without pattern-matching colours.
HEAD_MARK0 = 0xE7F5                      # h1..h6 -> E7F5..E7FA
_HEAD_MARK = re.compile('[-]')
RULE_MARK = chr(0xE7FB)                  # the hr placeholder in both styles
RULE_LIGHT = '─'
RULE_HEAVY = '━'
_RULE_CHARS = set(RULE_LIGHT + RULE_HEAVY + '-═')
_TRAIL_SGR = re.compile(r'(?:\x1b\[[0-9;]*m)+$')


def _rule(width, char, sgr):
    return sgr + char * max(int(width), 1) + SGR_OFF


def _visually_blank(line):
    return not SGR.sub('', line).strip()


def _is_rule(line):
    t = SGR.sub('', line).strip()
    return bool(t) and set(t) <= _RULE_CHARS


def expand_rules(text, width):
    """Draw the thematic break the styles marked, at the width of the pane.
    A literal rule in glamour's `format` string is fixed text: eight dashes is a
    stub at 100 columns and the same eight dashes at 58, so the break never
    reads as a break. Running first also means every later pass sees an ordinary
    rule line, which they all already skip."""
    return '\n'.join(
        _rule(width, RULE_LIGHT, SGR_RULE) if RULE_MARK in ln else ln
        for ln in text.split('\n')
    )


def style_headings(text, width):
    """Turn the marked heading levels into a hierarchy a reader can find while
    scrolling, and set the page's vertical rhythm while we are there.

    glamour separates heading levels by colour alone, which works when a page is
    read and disappears when it is scanned: at 58 columns every section title
    looks like one more line of prose. So each level gets structure as well as
    colour. An H1 is a masthead: the bar glamour already prefixes, then a heavy
    rule across the pane. An H2 opens with a light full-width rule ABOVE the
    title, so a new section announces itself before its words are read and the
    previous one is visibly closed. H3 and deeper keep colour alone, which is
    what gives the ladder a bottom instead of three shouting levels.

    Spacing is normalised in the same pass because it is the same decision:
    blank lines above a heading are collapsed and re-added deliberately (two
    above a heading that carries a rule, one above the rest), so rhythm comes
    from the level rather than from how the author happened to space the source.
    A rule already sitting above a heading (the author wrote `---`) is reused
    rather than doubled."""
    lines = text.split('\n')
    out = []
    for ln in lines:
        m = _HEAD_MARK.search(ln)
        if not m:
            out.append(ln)
            continue
        level = ord(m.group()) - HEAD_MARK0 + 1
        ln = ln[:m.start()] + ln[m.end():]
        while out and _visually_blank(out[-1]):
            out.pop()
        if not out:
            out.append('')                      # keep the pane's top margin
        elif _is_rule(out[-1]):
            # The author wrote their own break here. Reuse it as the section
            # rule rather than stacking a second one, and give it the lead-in a
            # rule we drew ourselves would have had.
            keep = out.pop()
            while out and _visually_blank(out[-1]):
                out.pop()
            out.extend(('', '') if out else ('',))
            out.append(keep)
        elif level <= 2:
            # A section break has to out-weigh the gap between two items inside
            # a section, or the page reads as one undifferentiated column.
            out.extend(('', ''))
        else:
            out.append('')
        if level == 2 and not _is_rule(out[-1]):
            out.append(_rule(width, RULE_LIGHT, SGR_RULE))
        # Teal on a leading "2." / "A.". glow wraps the number in its
        # own SGR, so allow codes between the digits and the period.
        ln = re.sub(
            r'^((?:\x1b\[[0-9;]*m)*)(\d+|[A-Z])((?:\x1b\[[0-9;]*m)*)(\.)((?:\x1b\[[0-9;]*m)*)',
            r'\1' + SGR_HNUM + r'\2\4' + SGR_OFF,
            ln, count=1)
        out.append(ln)
        if level == 1:
            out.append(_rule(width, RULE_HEAVY, SGR_QUIET))
    return '\n'.join(out)


def restyle_quote_gutters(text):
    """Paint the blockquote rail in the structural colour rather than the body
    text colour glamour gives it. The rail is furniture; at the weight of the
    prose it reads as a character in the sentence and the quote stops looking
    like a quote at a glance. The SGR codes glamour left immediately after the
    rail are re-emitted untouched, so the quote's own colour and italic still
    open exactly where they did."""
    out = []
    for ln in text.split('\n'):
        if _has_gutter(ln):
            prefix, _ = _split_leading(ln)
            if prefix:
                tail = _TRAIL_SGR.search(prefix)
                out.append(SGR_RULE + SGR.sub('', prefix) + SGR_OFF
                           + (tail.group() if tail else '') + ln[len(prefix):])
                continue
        out.append(ln)
    return '\n'.join(out)


def regutter_quote_orphans(text):
    """glamour wraps a blockquote but occasionally drops the `│ ` gutter on an
    overflow word, leaving it flush-left at column 0 between two gutter lines
    (the reported "for"/"bar." orphan). Re-add the gutter to any gutterless,
    non-indented line that sits sandwiched between blockquote lines, copying the
    coloured gutter from the preceding quote line so it matches. Only sandwiched
    lines are touched, so a normal paragraph after a quote is never affected."""
    lines = text.split('\n')
    n = len(lines)
    for i in range(n):
        s = SGR.sub('', lines[i])
        if not s.strip() or s[:1] == ' ' or _has_gutter(lines[i]):
            continue
        if i == 0 or not _has_gutter(lines[i - 1]):
            continue
        j = i + 1
        while j < n and not SGR.sub('', lines[j]).strip():
            j += 1
        if j < n and _has_gutter(lines[j]):
            prefix, _ = _split_leading(lines[i - 1])
            if prefix:
                lines[i] = prefix + lines[i]
    return '\n'.join(lines)


# Trailing padding at end-of-line: the ENTIRE run of spaces and SGR codes
# after the last visible glyph. glow colours each padding space (plain text
# colour + reset per space); stripping only the spaces left their code pairs
# behind, and after a paragraph join those orphaned codes overrode the quote
# italic for the following words (the "italics don't hold" bug).
_TRAIL_WS = re.compile(r'(?:\x1b\[[0-9;]*m| )+$')

# Characters glamour will break a long word at, so a single token can arrive as
# two rendered lines: `2026-08-` + `11_Notes.md`, `CARRY-` + `OVER:`,
# `Prep_Notes.` + `md`. Re-joining every fragment with a space would bake
# those splits in as `2026-08- 11_Notes.md`.
_WORD_BREAKPOINT = '-./,_'


def _tail_token(s):
    return s[s.rfind(' ') + 1:]


def _head_token(s):
    i = s.find(' ')
    return s if i < 0 else s[:i]


def _joins_tight(prev, nxt, src):
    """True when two rendered fragments are the two halves of ONE source token,
    so re-joining them must not insert a space. The source text is the oracle
    rather than a punctuation heuristic: `Prep_Notes.` + `md` joins tight because
    `Prep_Notes.md` appears in the source and `Prep_Notes. md` does not, while
    `sentence.` + `Next` keeps its space for exactly the same reason. Anything
    the source cannot settle (markdown syntax split the token, no source
    available) falls back to the space join, i.e. the previous behaviour."""
    if not src:
        return False
    p = SGR.sub('', prev)
    n = SGR.sub('', nxt)
    if not p or not n:
        return False
    if p[-1] not in _WORD_BREAKPOINT or len(p) < 2 or not p[-2].isalnum():
        return False
    if not n[0].isalnum():
        return False
    tail, head = _tail_token(p), _head_token(n)
    return (tail + head) in src and (tail + ' ' + head) not in src


def join_wrapped(segments, src=''):
    """Undo one rendered block's line wrapping. Fragments join with a space
    unless _joins_tight says they are halves of a single token."""
    out = ''
    for seg in segments:
        if not out:
            out = seg
        elif _joins_tight(out, seg, src):
            out += seg
        else:
            out += ' ' + seg
    return out


def rewrap_quote_runs(text, width, src=''):
    """Re-wrap blockquote paragraphs from scratch. glamour's word-wrap stumbles
    inside quotes: it strands single words on their own line ("3", "these",
    "fail") with full-width lines on both sides, so no local merge can fix it.
    For each run of gutter lines: split into paragraphs at bare-gutter rows,
    strip each line's gutter, join the paragraph into one long line (visible
    trailing padding removed, SGR codes kept in place), and hand it to
    _fold_core, which wraps at spaces and carries colour state across breaks.
    Every line gets the run's gutter prefix back. Non-quote lines pass through
    untouched."""
    lines = text.split('\n')
    out = []
    i = 0
    n = len(lines)
    while i < n:
        if not _has_gutter(lines[i]):
            out.append(lines[i])
            i += 1
            continue
        run = []
        while i < n and _has_gutter(lines[i]):
            run.append(lines[i])
            i += 1
        prefix, pw = _split_leading(run[0])
        # One extra column of slack: the prefix/leading-space accounting can
        # land a segment exactly one over, and the viewer's 2-column margin
        # reserve absorbs nothing at render level, so stay strictly inside.
        body_w = width - pw - 1
        if len(run) < 2 or body_w < 16 or not prefix:
            out.extend(run)
            continue
        para = []
        def flush():
            if para:
                joined = join_wrapped(para, src)
                out.extend(prefix + seg for seg in _fold_core(joined, body_w))
                para.clear()
        for ln in run:
            p, _ = _split_leading(ln)
            body = _TRAIL_WS.sub('', ln[len(p):])
            if not SGR.sub('', body).strip():
                flush()
                out.append(prefix)          # keep the bare-gutter separator row
            else:
                para.append(body)
        flush()
    return '\n'.join(out)


# Flush-left prose line: candidate for the widow repair below. Excludes
# anything indented, gutter/table lines, bullet or numbered list markers, and
# horizontal rules. List item CONTINUATION lines do reach here (glow emits them
# flush-left), but the marker line above and below breaks the run, so a
# continuation only ever joins with its own sibling continuations.
_LIST_MARK = re.compile(r'^(?:[•▪◦‣·*+\-▸✓]\s|\d{1,3}[.)]\s)')


def _prose_body(line):
    """ANSI-stripped, right-trimmed text of a flush-left prose line, or None
    when the line is blank, indented, a quote/table, a list marker, or a rule."""
    s = SGR.sub('', line)
    t = s.rstrip()
    if not t or s[:1] in (' ', '\t'):
        return None
    if '│' in t or _LIST_MARK.match(t):
        return None
    if set(t) <= set('─━-═ '):
        return None
    return t


def rewrap_prose_widows(text, width, src=''):
    """Repair glamour's width-specific widow bug in PLAIN paragraphs (the quote
    variant is rewrap_quote_runs). At certain widths glamour strands words of a
    flush-left paragraph on their own line (one repro at `-w 75` ended a
    paragraph '... we do' / 'anything' / 'else with them.'). Greedy wrap never
    emits a line that could still absorb the
    next line's first word, so a run violating that invariant is the bug: join
    the whole run (trailing padding stripped, SGR kept) and re-wrap it with
    _fold_core at the width glow was asked for. Runs holding the invariant pass
    through byte-identical, which also keeps intentional hard-break formatting
    intact outside a buggy paragraph."""
    lines = text.split('\n')
    out = []
    i = 0
    n = len(lines)
    while i < n:
        if _prose_body(lines[i]) is None:
            out.append(lines[i])
            i += 1
            continue
        run, texts = [], []
        while i < n:
            b = _prose_body(lines[i])
            if b is None:
                break
            run.append(lines[i])
            texts.append(b)
            i += 1
        # STRICTLY under width: both glamour and _fold_core sometimes wrap a
        # word that would have ended exactly AT the limit (an off-by-one both
        # sides share), and firing on that equality case would rejoin nearly
        # every paragraph. The real bug leaves whole words of slack.
        bad = any(
            text_width(texts[k]) + 1 + text_width(texts[k + 1].split()[0]) < width
            for k in range(len(texts) - 1)
        )
        if not bad:
            out.extend(run)
            continue
        joined = join_wrapped([_TRAIL_WS.sub('', ln) for ln in run], src)
        out.extend(_fold_core(joined, width))
    return '\n'.join(out)


# ---------------------------------------------------------------------------
# Lists: hanging indents, and GitHub task lists rendered as a terminal checklist
# ---------------------------------------------------------------------------
# glamour collapses GitHub task items ("- [ ] ...") into ONE run-together
# paragraph carrying literal "[ ]" text: ten checklist items arrive as a wall.
# mark_task_items() rewrites each task item into a plain list item whose text
# starts with a private-use marker BEFORE glow sees the file, so glamour lays the
# items out as a real list; rewrap_list_items() then swaps glamour's "• " bullet
# for the glyphs below and re-wraps every list item with a hanging indent.
#
# Glyphs, chosen for a narrow Rose Pine pane with a Nerd Font: a pending item is
# a bold gold ▸ (reads as "still on you", and does not collide with the plain •
# bullet used elsewhere); a done item is a calm pine ✓ over muted text, so the
# resolved block recedes as a block instead of competing with the live one.
TASK_OPEN = chr(0xE7F0)    # inside the range _PUA_STRAY already sweeps
TASK_DONE = chr(0xE7F1)
GLYPH_OPEN = '▸'
GLYPH_DONE = '✓'

# Plain list markers, one step lighter per nesting depth. glamour prints the
# same "• " at every level, so depth was legible only by counting leading
# spaces; stepping the glyph down makes a nested list read as nested.
GLYPH_BULLET = ('•', '◦', '·')
_BULLET_GLYPHS = '•▪◦‣·'

# A status-labelled aside at the end of a list item: "(READY: ...)",
# "(CARRY-OVER: ...)", "(NOTE, ...)". The SHAPE is the rule, never a list of
# known words: an ALL-CAPS token of two or more characters (hyphens and slashes
# allowed inside), a colon or comma, then the aside itself. The label is painted
# as a tag and the rest is dropped to the quiet colour, so an annotation that
# runs three lines long stops competing with the sentence it annotates.
_ANNOT = re.compile(r'\(([A-Z][A-Z0-9]+(?:[-/][A-Z0-9]+)*)([:,])(\s.*)\)\s*$')

_FENCE = re.compile(r'^ {0,3}(```+|~~~+)')
_TASK_LINE = re.compile(r'^( *[-*+][ \t]+)\[([ xX])\][ \t]+(?=\S)')
_ANY_ITEM = re.compile(r'^( *)(?:[-*+]|\d{1,9}[.)])[ \t]')


def mark_task_items(text):
    """Swap "- [ ] " / "- [x] " for a plain list marker plus a task marker char.
    Returns (new_text, changed).

    Two places keep their literal checkboxes, because there the text is content
    rather than a list: fenced code, and an indented code block (4+ columns with
    no open list to nest under, so `    - [ ] x` under a paragraph stays as
    typed while `  - [ ] x` under a list item is a real nested task)."""
    out = []
    fence = None
    changed = False
    list_indent = None
    for ln in text.split('\n'):
        f = _FENCE.match(ln)
        if f:
            if fence is None:
                fence = f.group(1)[0] * 3
            elif ln.lstrip().startswith(fence):
                fence = None
            out.append(ln)
            continue
        if fence is not None or not ln.strip():
            out.append(ln)
            continue
        indent = len(ln) - len(ln.lstrip(' '))
        limit = 3 if list_indent is None else list_indent + 3
        item = _ANY_ITEM.match(ln)
        if item and indent <= limit:
            list_indent = indent
            m = _TASK_LINE.match(ln)
            if m:
                ln = (m.group(1)
                      + (TASK_DONE if m.group(2) in 'xX' else TASK_OPEN)
                      + ln[m.end():])
                changed = True
        elif indent == 0:
            list_indent = None
        out.append(ln)
    return '\n'.join(out), changed


# A rendered list item: optional indent, then glamour's bullet ("• " from the
# style's item.block_prefix) or an enumeration marker ("1. ").
_ITEM_MARK = re.compile(r'^( *)((?:[•▪◦‣] )|(?:\d{1,3}\. ))')


def _item_head(line, in_list):
    """(indent, marker_width, raw_marker_prefix, body, marker_text) for a
    rendered list-item line, else None. A numeric marker only counts when a list
    is already open or the block just started, so a paragraph wrapping onto
    "1. " is left alone."""
    vis = SGR.sub('', line)
    if '│' in vis:
        return None
    m = _ITEM_MARK.match(vis)
    if not m:
        return None
    if m.group(2)[0].isdigit() and not in_list:
        return None
    cut = _peel_visible(line, len(m.group(0)))
    return (len(m.group(1)), len(m.group(2)), line[:cut], line[cut:], m.group(2))


def _is_continuation(line, indent):
    """True for a wrapped-item continuation: glamour emits these at the item
    MARKER's own indent, non-blank, with no gutter/table pipe and no rule."""
    vis = SGR.sub('', line)
    t = vis.rstrip()
    if not t or '│' in t:
        return False
    if len(vis) - len(vis.lstrip(' ')) != indent:
        return False
    return not set(t) <= set('─━-═ ')


# Visible trailing spaces, keeping any SGR reset that closes the line: _fold_core
# leaves the space it broke at on the head piece, and a line that exactly fills
# the pane leaves the terminal in pending-wrap state.
_TRAIL_SPACE = re.compile(r' +(?=(?:\x1b\[[0-9;]*m)*$)')


def accent_annotation(joined):
    """Re-rank a status-labelled aside at the end of a list item.

    An item like "**Do the thing** *(READY: three paragraphs of why)*" arrives
    with the aside at the same weight as the sentence it hangs off, so the eye
    has no idea which half is the instruction. Here the label becomes a tag and
    the aside drops to the quiet colour: the item's own words lead, the status
    word is findable at a glance down the column, and the explanation stays
    readable without asking to be read. Nothing about the item is matched by
    name; see _ANNOT for the shape. Whatever styling glamour gave the aside is
    replaced rather than layered, so an aside glamour rendered bold and one it
    rendered italic end up looking the same."""
    plain = SGR.sub('', joined)
    m = _ANNOT.search(plain)
    if not m or m.start() == 0:
        return joined
    # Drop the codes that opened the aside glamour is about to lose: they style
    # only the text being repainted, so carrying them would leave a dead colour
    # change sitting in the stream ahead of the one that wins.
    head = _TRAIL_SGR.sub('', joined[:_peel_visible(joined, m.start())])
    return '%s%s%s(%s%s%s%s%s)%s' % (
        head, SGR_OFF,
        SGR_QUIET, SGR_LABEL, m.group(1) + m.group(2), SGR_OFF,
        SGR_QUIET, m.group(3), SGR_OFF)


def rewrap_list_items(text, width, src=''):
    """Re-wrap every rendered list item with a hanging indent, and turn the
    marked task items into the checklist glyphs.

    glamour wraps a list item back to column 0 (or to the marker's own indent for
    a nested one), so a three-line item reads as a paragraph with a bullet stuck
    on the front. Each item is joined back into one line, re-wrapped to `width`,
    and given continuation lines indented under its text. Task items additionally
    get their bullet replaced by the state glyph; a done item is flattened to one
    muted colour so it recedes. When any task item in a run wraps past one line,
    the run is blank-line separated so one item never runs into the next."""
    lines = text.split('\n')
    out = []
    i = 0
    n = len(lines)
    in_list = False
    prev_blank = True
    run = []            # rendered blocks of the current task-item run
    run_wrapped = False
    run_indent = -1

    def flush_run():
        nonlocal run, run_wrapped, run_indent
        for k, block in enumerate(run):
            if k and run_wrapped:
                out.append('')
            out.extend(block)
        run = []
        run_wrapped = False
        run_indent = -1

    while i < n:
        head = _item_head(lines[i], in_list or prev_blank)
        if head is None:
            prev_blank = not SGR.sub('', lines[i]).strip()
            if prev_blank:
                in_list = False
            flush_run()
            out.append(lines[i])
            i += 1
            continue
        indent, mark_w, prefix, body, mark = head
        segs = [_TRAIL_WS.sub('', body)]
        i += 1
        while i < n and _item_head(lines[i], True) is None and _is_continuation(lines[i], indent):
            # Peel the indent SGR-aware: glamour wraps even the leading spaces in
            # colour codes, so a plain lstrip removes nothing and the spaces ride
            # into the join as extra width.
            cont = lines[i][_peel_visible(lines[i], indent):]
            segs.append(_TRAIL_WS.sub('', cont))
            i += 1
        in_list = True
        prev_blank = False

        joined = join_wrapped([s for s in segs if SGR.sub('', s).strip()], src)
        plain = SGR.sub('', joined)
        kind = plain[:1] if plain[:1] in (TASK_OPEN, TASK_DONE) else ''
        if kind:
            joined = joined.replace(kind, '', 1)
            if kind == TASK_DONE:
                joined = SGR.sub('', joined)
            prefix = '%s%s%s%s %s' % (
                ' ' * indent,
                SGR_ACCENT if kind == TASK_OPEN else SGR_DONE,
                GLYPH_OPEN if kind == TASK_OPEN else GLYPH_DONE,
                SGR_OFF,
                SGR_QUIET + SGR_STRIKE if kind == TASK_DONE else '')
            mark_w = 2
        else:
            # Plain markers are furniture too. Quiet them so the item's own text
            # leads the line, and step the glyph down by depth so a nested list
            # reads as nested instead of as an indented sibling.
            glyph = mark
            if mark[:1] in _BULLET_GLYPHS:
                glyph = GLYPH_BULLET[min(indent // 2, len(GLYPH_BULLET) - 1)] + mark[1:]
            # The codes trailing the old marker are the BODY's opening colour
            # (_peel_visible absorbs whatever sits between the marker and the
            # first word). They move to the front of the item's text rather than
            # onto the new marker, which both keeps the text styled and fixes a
            # standing bug: parked on the marker, the colour died at the first
            # fold, so the second line of a bold lead-in dropped to the
            # terminal's own foreground mid-phrase.
            tail = _TRAIL_SGR.search(prefix)
            joined = (tail.group() if tail else '') + joined
            prefix = '%s%s%s%s%s' % (' ' * indent, SGR_QUIET, glyph.rstrip(),
                                     SGR_OFF, ' ' * (len(glyph) - len(glyph.rstrip())))
        if kind != TASK_DONE:
            joined = accent_annotation(joined)

        pad = ' ' * (indent + mark_w)
        body_w = width - indent - mark_w
        pieces = _fold_core(joined, body_w) if body_w > 8 else [joined]
        pieces = [_TRAIL_SPACE.sub('', p) for p in pieces]
        if kind == TASK_DONE:
            block = [prefix + pieces[0]] + [pad + SGR_QUIET + SGR_STRIKE + p for p in pieces[1:]]
        else:
            block = [prefix + pieces[0]] + [pad + p for p in pieces[1:]]
        # Close every styled line. The join strips each source line's trailing
        # reset, so without this the last piece of an italic annotation would
        # bleed its colour onto whatever the terminal paints next.
        block = [b + SGR_OFF if SGR.search(b) and not b.endswith(SGR_OFF) else b
                 for b in block]

        if kind:
            # Only same-level items separate as one run, so a nested child never
            # gets a blank line driven between it and its parent.
            if run and run_indent != indent:
                flush_run()
            run_indent = indent
            run.append(block)
            run_wrapped = run_wrapped or len(block) > 1
        else:
            flush_run()
            out.extend(block)
    flush_run()
    return '\n'.join(out)


def fit_width(text, width):
    """Fold every line of a rendered document to `width` visible columns so no
    line can exceed the pane and get hard-wrapped mid-word by the terminal."""
    out = []
    for ln in text.split('\n'):
        out.extend(fold_line(ln, width))
    return '\n'.join(out)

# glow does not merely emit those queries, it BLOCKS ~5s on each waiting for the
# terminal to answer (OSC 11 bg, then OSC 10 fg, then OSC 11 again, each with a
# CSI 6n), which is a fixed ~15s freeze per render under our answer-less pty.
# We answer them the instant they arrive so glow proceeds immediately. Replies
# are written to the pty master (glow's stdin); pty ECHO is disabled first so the
# replies do not bounce back into the captured output. Patterns and canonical
# replies (bytes):
Q_OSC_BG = re.compile(rb'\x1b\]11;\?(?:\x07|\x1b\\)')  # background colour query
Q_OSC_FG = re.compile(rb'\x1b\]10;\?(?:\x07|\x1b\\)')  # foreground colour query
Q_CPR = re.compile(rb'\x1b\[6n')                        # cursor position report
Q_DA = re.compile(rb'\x1b\[(?:>|=)?c')                  # device attributes
Q_DSR = re.compile(rb'\x1b\[5n')                        # device status report
# Reply with the ACTUAL background/foreground for the current appearance so glow
# never assumes the wrong contrast (16-bit-per-channel xterm rgb: form).
if DARK:
    R_OSC_BG = b'\x1b]11;rgb:1919/1717/2424\x1b\\'      # Rose Pine Main base #191724
    R_OSC_FG = b'\x1b]10;rgb:e0e0/dede/f4f4\x1b\\'      # Rose Pine text #e0def4
else:
    R_OSC_BG = b'\x1b]11;rgb:fafa/f4f4/eded\x1b\\'      # Rose Pine Dawn base #faf4ed
    R_OSC_FG = b'\x1b]10;rgb:5757/5252/7979\x1b\\'      # Rose Pine Dawn text #575279
R_CPR = b'\x1b[1;1R'
R_DA = b'\x1b[?62;c'
R_DSR = b'\x1b[0n'

# Environment that makes glow emit 24-bit colour under a pty.
# PAGER=cat is the no-pager guard. glow 2.1.2 has no usable --pager=false
# (it pages when the flag is present, even set to false). yaml pager:true
# plus the default `less -r` waits forever on this PTY; cat dumps and exits.
COLOR_ENV = {
    'COLORTERM': 'truecolor',
    'TERM': 'xterm-256color',
    'CLICOLOR_FORCE': '1',
    'CLICOLOR': '1',
    'FORCE_COLOR': '1',
    'PAGER': 'cat',
}


def _color_env(base=None):
    """Force truecolor for glow even if the parent shell exported NO_COLOR
    (some tool wrappers do). Empty-string NO_COLOR is not enough; glow
    treats the variable's presence as off."""
    env = dict(base if base is not None else os.environ)
    env.update(COLOR_ENV)
    env.pop('NO_COLOR', None)
    return env


def parse_args(argv):
    cols = '100'
    style = None
    path = None
    img = {'on': False, 'pane_rows': 0, 'out': None, 'cell': None}
    i = 0
    while i < len(argv):
        a = argv[i]
        # --width is an alias for --columns. Without it an unrecognised flag fell
        # through to the `path` branch, so `--width 58 file.md` silently rendered
        # at the 100-column default instead of failing loudly.
        if a in ('--columns', '--width'):
            cols = argv[i + 1]
            i += 2
        elif a == '--style':
            style = argv[i + 1]
            i += 2
        elif a == '--images':
            img['on'] = True
            i += 1
        elif a == '--pane-rows':
            img['pane_rows'] = int(argv[i + 1]) if argv[i + 1].isdigit() else 0
            i += 2
        elif a == '--images-out':
            img['out'] = argv[i + 1]
            i += 2
        elif a == '--cell-px':
            img['cell'] = argv[i + 1]
            i += 2
        else:
            path = a
            i += 1
    return cols, style, path, img


# H1 and H2 render with a left bar prefix ("▌ ") in both bundled styles; H3
# and deeper carry colour only. The bar has to stay the first visible glyph of
# a heading line: sections.py maps a rendered heading back to its source line
# by lstrip('▌') and an exact title match, which is also why the section rules
# this file draws go on their own lines rather than into the title.


def resolve_style(style):
    """--style flag wins, then $MDW_GLOW_STYLE, then the bundled style."""
    cand = style or os.environ.get('MDW_GLOW_STYLE') or DEFAULT_STYLE
    return cand if os.path.isfile(cand) else DEFAULT_STYLE


def run_pipe(cmd, text=None):
    """Run a renderer over stdin/args via a pipe; stdout on success else None.

    Two guards against a wedged renderer holding the live viewer: stdin is
    /dev/null when there is nothing to feed it (glow otherwise waits on a
    terminal reply that will never arrive down a pipe), and the call carries
    PIPE_TIMEOUT so an overrun falls through to the next stage of the chain
    instead of freezing the pane."""
    env = _color_env()
    kw = {'input': text} if text is not None else {'stdin': subprocess.DEVNULL}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, env=env,
                           timeout=PIPE_TIMEOUT, **kw)
        return p.stdout if p.returncode == 0 and p.stdout else None
    except subprocess.TimeoutExpired:
        _log('run_pipe timed out after %ss: %s' % (PIPE_TIMEOUT, ' '.join(cmd)))
        return None
    except Exception:
        return None


def _answer_queries(data):
    """Given a chunk glow wrote, return the reply bytes for any terminal queries
    it contains so glow does not block waiting for the real terminal."""
    reply = b''
    if Q_OSC_BG.search(data):
        reply += R_OSC_BG
    if Q_OSC_FG.search(data):
        reply += R_OSC_FG
    if Q_CPR.search(data):
        reply += R_CPR
    if Q_DA.search(data):
        reply += R_DA
    if Q_DSR.search(data):
        reply += R_DSR
    return reply


def glow_pty(cmd):
    """Run glow under a pseudo-terminal so it emits truecolour instead of
    degrading to 16-colour ANSI (which it does whenever stdout is a pipe).
    Answers glow's startup terminal-detection queries so it does not block ~5s
    each (a fixed ~15s freeze otherwise). Returns decoded output with the pty's
    \\r stripped and the query bytes removed, or None on failure/timeout."""
    try:
        pid, fd = pty.fork()
    except Exception:
        return None
    if pid == 0:  # child
        env = _color_env()
        os.environ.clear()
        os.environ.update(env)
        try:
            os.execvp(cmd[0], cmd)
        except Exception:
            os._exit(127)
    # parent: turn off ECHO so the replies we inject do not echo back into the
    # captured stream (which would reintroduce the escape-response leak).
    try:
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~(termios.ECHO | termios.ECHONL)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except Exception:
        pass
    chunks = []
    t0 = time.time()
    timed_out = False
    while True:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break  # EIO on child exit is normal for ptys
            if not data:
                break
            chunks.append(data)
            reply = _answer_queries(data)
            if reply:
                try:
                    os.write(fd, reply)
                except OSError:
                    pass
        else:
            # No output pending: if the child has exited, do a final non-blocking
            # drain and stop. Otherwise keep waiting until the hard backstop.
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)
            except OSError:
                done = pid
            if done != 0:
                while True:
                    r2, _, _ = select.select([fd], [], [], 0)
                    if not r2:
                        break
                    try:
                        d = os.read(fd, 65536)
                    except OSError:
                        break
                    if not d:
                        break
                    chunks.append(d)
                break
        if time.time() - t0 > PTY_TIMEOUT:
            timed_out = True
            try:
                os.kill(pid, 9)
            except OSError:
                pass
            break
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass
    if timed_out:
        return None  # let the fallback chain try rather than emit a partial mess
    out = b''.join(chunks).decode('utf-8', 'replace').replace('\r\n', '\n').replace('\r', '')
    out = TERM_QUERY.sub('', out)
    return out if out.strip() else None


# Raw C0/C1 control bytes that must never reach the terminal live. A markdown
# file should not carry terminal control codes; if one does (accident or an
# injection attempt), neutralise it to visible caret notation so it is shown as
# text, never executed. Keep \n (handled by lines) and \t (expanded separately).
_C0 = {c for c in range(0x00, 0x20)} | {0x7f}
_C0.discard(0x09)  # tab, expanded below
_C0.discard(0x0a)  # newline


def sanitize_text(text):
    """Expand tabs and turn raw control bytes into visible caret notation.
    Returns (clean_text, changed)."""
    if not any((ord(ch) in _C0) or ch == '\t' for ch in text):
        return text, False
    out = []
    for ch in text:
        o = ord(ch)
        if ch == '\t':
            out.append('    ')
        elif o in _C0:
            # caret notation: ESC -> ^[, other C0 -> ^X, DEL -> ^?
            out.append('^' + chr((o + 0x40) % 0x80) if o != 0x7f else '^?')
        else:
            out.append(ch)
    return ''.join(out), True


# Clickable links (OSC 8). glow gives these up (it prints the
# raw URL, underlined, and the fold then hard-wraps long URLs mid-string, which
# also defeats Ghostty's plain-URL detection). We restore them around glow:
# extract_links() swaps every inline markdown link for its text plus two
# private-use marker characters BEFORE glow renders (so glow styles the text as
# ordinary prose and never sees the URL), then linkify() replaces the markers in
# the final folded output with real OSC 8 hyperlink sequences plus an underline.
# Single-character markers cannot be split by the folder, and each adds width 1
# during folding, so lines only ever come out narrower than the target, never
# wider. Ghostty renders OSC 8 as cmd+clickable text. Disable: MDW_LINKS=0.
# Stray markers (e.g. a renderer fallback that bypassed linkify) are stripped in
# emit() so they can never reach the pane as tofu.
LINKS_ENABLED = os.environ.get('MDW_LINKS', '1') != '0'
_LINK_CLOSE = ''              # one shared close marker
_LINK_OPEN0 = 0xE100                # open marker for link i is chr(_LINK_OPEN0+i)
_LINK_MAX = 1500                    # E100..E6DB, well inside the PUA
_MD_LINK = re.compile(r'(?<!\!)\[([^\]\[\n]+)\]\((https?://[^)\s]+)\)')
_PUA_STRAY = re.compile('[-]')


def extract_links(text):
    """Replace inline markdown links with marker-wrapped text. Returns
    (new_text, urls) where urls[i] belongs to open marker chr(_LINK_OPEN0+i)."""
    urls = []

    def repl(m):
        if len(urls) >= _LINK_MAX:
            return m.group(0)
        # OSC payloads must stay free of control bytes; drop any (there should
        # be none after sanitize_text, this is belt and braces).
        url = ''.join(ch for ch in m.group(2) if ord(ch) >= 32)
        urls.append(url)
        return chr(_LINK_OPEN0 + len(urls) - 1) + m.group(1) + _LINK_CLOSE

    return _MD_LINK.sub(repl, text), urls


# Link ink is foam, Rose Pine's canonical link colour.
SGR_LINK = _sgr('#9ccfd8') if DARK else _sgr('#56949f')
SGR_LINK_ON = SGR_LINK + '\x1b[4m'
# glow paints every token (it splits on `_`, `-`, `.`) as its own SGR wrap
# plus a reset. The OSC 8 span still covers the whole label, but the first
# reset drops our blue/underline and the rest of the title goes body-gray.
# Re-assert ink after every SGR that lands between the markers.
_OSC8_TO_CLOSE = re.compile(
    r'(\x1b\]8;;[^\x1b\x07]*(?:\x1b\\|\x07))(.*?)' + re.escape(_LINK_CLOSE),
    re.DOTALL)


def linkify(text, urls):
    """Swap the markers extract_links() planted for real OSC 8 hyperlinks.
    SGR resets inside the link text do not end the hyperlink (OSC 8 closes only
    at the empty OSC 8), so clickability survives glow's per-word colour codes.
    Colour and underline are re-asserted after those resets so a title like
    Q3_Planning_Notes_Final stays one coloured link."""
    for i, url in enumerate(urls):
        text = text.replace(chr(_LINK_OPEN0 + i), '\x1b]8;;%s\x1b\\' % url)

    def _paint(m):
        body = SGR.sub(lambda sm: sm.group(0) + SGR_LINK_ON, m.group(2))
        return m.group(1) + SGR_LINK_ON + body + '\x1b[24m\x1b[39m\x1b]8;;\x1b\\'

    return _OSC8_TO_CLOSE.sub(_paint, text)


# ---------------------------------------------------------------------------
# Inline images: kitty graphics protocol Unicode placeholders
# ---------------------------------------------------------------------------
# An image becomes rows of placeholder cells. Each cell is U+10EEEE followed by
# a row diacritic and a column diacritic (index = value, from kitty's
# rowcolumn-diacritics.txt, the table Ghostty's graphics_unicode.zig decodes),
# and the whole row is painted in a 24-bit foreground colour whose r,g,b bytes
# ARE the image id. Every cell carries both diacritics: mdw paints arbitrary
# windows of the buffer, so no row may depend on inference from a row above it.
# The terminal draws the matching slice of the image into each cell, so the
# picture behaves like text: it scrolls, clips at the pane edges, and repaints
# with the line it sits on.
IMG_PLACEHOLDER = chr(0x10EEEE)
_DIACRITICS = [chr(c) for c in (
    0x0305, 0x030D, 0x030E, 0x0310, 0x0312, 0x033D, 0x033E, 0x033F, 0x0346, 0x034A,
    0x034B, 0x034C, 0x0350, 0x0351, 0x0352, 0x0357, 0x035B, 0x0363, 0x0364, 0x0365,
    0x0366, 0x0367, 0x0368, 0x0369, 0x036A, 0x036B, 0x036C, 0x036D, 0x036E, 0x036F,
    0x0483, 0x0484, 0x0485, 0x0486, 0x0487, 0x0592, 0x0593, 0x0594, 0x0595, 0x0597,
    0x0598, 0x0599, 0x059C, 0x059D, 0x059E, 0x059F, 0x05A0, 0x05A1, 0x05A8, 0x05A9,
    0x05AB, 0x05AC, 0x05AF, 0x05C4, 0x0610, 0x0611, 0x0612, 0x0613, 0x0614, 0x0615,
    0x0616, 0x0617, 0x0657, 0x0658, 0x0659, 0x065A, 0x065B, 0x065D, 0x065E, 0x06D6,
    0x06D7, 0x06D8, 0x06D9, 0x06DA, 0x06DB, 0x06DC, 0x06DF, 0x06E0, 0x06E1, 0x06E2,
    0x06E4, 0x06E7, 0x06E8, 0x06EB, 0x06EC, 0x0730, 0x0732, 0x0733, 0x0735, 0x0736,
    0x073A, 0x073D, 0x073F, 0x0740, 0x0741, 0x0743, 0x0745, 0x0747, 0x0749, 0x074A,
    0x07EB, 0x07EC, 0x07ED, 0x07EE, 0x07EF, 0x07F0, 0x07F1, 0x07F3, 0x0816, 0x0817,
    0x0818, 0x0819, 0x081B, 0x081C, 0x081D, 0x081E, 0x081F, 0x0820, 0x0821, 0x0822,
    0x0823, 0x0825, 0x0826, 0x0827, 0x0829, 0x082A, 0x082B, 0x082C, 0x082D, 0x0951,
    0x0953, 0x0954, 0x0F82, 0x0F83, 0x0F86, 0x0F87, 0x135D, 0x135E, 0x135F, 0x17DD,
    0x193A, 0x1A17, 0x1A75, 0x1A76, 0x1A77, 0x1A78, 0x1A79, 0x1A7A, 0x1A7B, 0x1A7C,
    0x1B6B, 0x1B6D, 0x1B6E, 0x1B6F, 0x1B70, 0x1B71, 0x1B72, 0x1B73, 0x1CD0, 0x1CD1,
    0x1CD2, 0x1CDA, 0x1CDB, 0x1CE0, 0x1DC0, 0x1DC1, 0x1DC3, 0x1DC4, 0x1DC5, 0x1DC6,
    0x1DC7, 0x1DC8, 0x1DC9, 0x1DCB, 0x1DCC, 0x1DD1, 0x1DD2, 0x1DD3, 0x1DD4, 0x1DD5,
    0x1DD6, 0x1DD7, 0x1DD8, 0x1DD9, 0x1DDA, 0x1DDB, 0x1DDC, 0x1DDD, 0x1DDE, 0x1DDF,
    0x1DE0, 0x1DE1, 0x1DE2, 0x1DE3, 0x1DE4, 0x1DE5, 0x1DE6, 0x1DFE, 0x20D0, 0x20D1,
    0x20D4, 0x20D5, 0x20D6, 0x20D7, 0x20DB, 0x20DC, 0x20E1, 0x20E7, 0x20E9, 0x20F0,
    0x2CEF, 0x2CF0, 0x2CF1, 0x2DE0, 0x2DE1, 0x2DE2, 0x2DE3, 0x2DE4, 0x2DE5, 0x2DE6,
    0x2DE7, 0x2DE8, 0x2DE9, 0x2DEA, 0x2DEB, 0x2DEC, 0x2DED, 0x2DEE, 0x2DEF, 0x2DF0,
    0x2DF1, 0x2DF2, 0x2DF3, 0x2DF4, 0x2DF5, 0x2DF6, 0x2DF7, 0x2DF8, 0x2DF9, 0x2DFA,
    0x2DFB, 0x2DFC, 0x2DFD, 0x2DFE, 0x2DFF, 0xA66F, 0xA67C, 0xA67D, 0xA6F0, 0xA6F1,
    0xA8E0, 0xA8E1, 0xA8E2, 0xA8E3, 0xA8E4, 0xA8E5, 0xA8E6, 0xA8E7, 0xA8E8, 0xA8E9,
    0xA8EA, 0xA8EB, 0xA8EC, 0xA8ED, 0xA8EE, 0xA8EF, 0xA8F0, 0xA8F1, 0xAAB0, 0xAAB2,
    0xAAB3, 0xAAB7, 0xAAB8, 0xAABE, 0xAABF, 0xAAC1, 0xFE20, 0xFE21, 0xFE22, 0xFE23,
    0xFE24, 0xFE25, 0xFE26, 0x10A0F, 0x10A38, 0x1D185, 0x1D186, 0x1D187, 0x1D188, 0x1D189,
    0x1D1AA, 0x1D1AB, 0x1D1AC, 0x1D1AD, 0x1D242, 0x1D243, 0x1D244,
)]
# Marker for an image paragraph between the source rewrite and the placeholder
# pass. Inside the _PUA_STRAY range, so a marker that somehow survives is swept
# instead of reaching the pane as tofu.
IMG_MARK = chr(0xE7FC)
_IMG_TOKEN = re.compile(IMG_MARK + r'(\d+)' + IMG_MARK)
# A standalone image line at column 0: ![alt](dest "optional title"). An
# indented one (inside a list item or under a paragraph) is left as text:
# rewrap_list_items would fold the marker into the item's own words.
_IMG_LINE = re.compile(
    r'^()!\[([^\]\n]*)\]\(\s*(<[^>\n]+>|[^)\s]+)'
    r'(?:\s+(?:"[^"\n]*"|\'[^\'\n]*\'))?\s*\)\s*$')
# One image never takes more than this share of the pane's rows.
IMG_MAX_FRACTION = 0.7
DEFAULT_CELL_PX = (9, 19)
_IMG_CACHE = os.path.join(os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache'),
                          'mdw', 'img')
_PNG_SIG = b'\x89PNG\r\n\x1a\n'


def _png_size(path):
    """(width, height) from a PNG's IHDR, or None if it is not a PNG."""
    try:
        with open(path, 'rb') as fh:
            head = fh.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != _PNG_SIG or head[12:16] != b'IHDR':
        return None
    w = int.from_bytes(head[16:20], 'big')
    h = int.from_bytes(head[20:24], 'big')
    return (w, h) if w > 0 and h > 0 else None


def _as_png(path, st):
    """Path of a PNG the terminal can decode for `path`: the file itself when it
    is a PNG, else a cached conversion keyed by path and mtime (macOS sips,
    else ImageMagick). The placement path sends PNG only, so JPEG, GIF, HEIC
    and friends go through here. None when no converter can read the file."""
    if _png_size(path):
        return path
    import hashlib
    key = hashlib.sha1(('%s\0%d\0%d' % (path, st.st_mtime_ns, st.st_size))
                       .encode('utf-8', 'replace')).hexdigest()
    out = os.path.join(_IMG_CACHE, key + '.png')
    if _png_size(out):
        return out
    try:
        os.makedirs(_IMG_CACHE, exist_ok=True)
    except OSError:
        return None
    for cmd in (['sips', '-s', 'format', 'png', path, '--out', out],
                ['magick', path + '[0]', out],
                ['convert', path + '[0]', out]):
        try:
            subprocess.run(cmd, capture_output=True, timeout=15)
        except Exception:
            continue
        if _png_size(out):
            return out
    return None


def _resolve_image(dest, base_dir):
    """Absolute path for a markdown image destination, or None for a URL."""
    import urllib.parse
    if dest.startswith('<') and dest.endswith('>'):
        dest = dest[1:-1]
    if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', dest) and not dest.startswith('file:'):
        return None                     # http(s), data:, mailto: and the like
    if dest.startswith('file://'):
        dest = dest[7:]
    dest = urllib.parse.unquote(dest)
    dest = os.path.expanduser(dest)
    if not os.path.isabs(dest):
        dest = os.path.join(base_dir, dest)
    return os.path.normpath(dest)


def _image_id(path, st, taken):
    """Stable 24-bit id from (path, mtime, size): the same file keeps its id
    across re-renders, so mdw transmits it once per session, and an edited
    image gets a new id and is sent again. Never 0 (0 means no image)."""
    import zlib
    iid = zlib.crc32(('%s\0%d\0%d' % (path, st.st_mtime_ns, st.st_size))
                     .encode('utf-8', 'replace')) & 0xFFFFFF
    iid = iid or 1
    while iid in taken:
        iid = (iid % 0xFFFFFF) + 1
    return iid


def extract_images(text, base_dir):
    """Swap each standalone, readable image line for a marker paragraph.
    Returns (new_text, images); images[i] belongs to marker i. Lines inside code
    fences, remote URLs, and missing or unreadable files are left exactly as
    written, so glow renders them the way it always has."""
    out = []
    images = []
    ids = {}
    fence = None
    for ln in text.split('\n'):
        f = _FENCE.match(ln)
        if f:
            if fence is None:
                fence = f.group(1)[0] * 3
            elif ln.lstrip().startswith(fence):
                fence = None
            out.append(ln)
            continue
        m = None if fence is not None else _IMG_LINE.match(ln)
        path = _resolve_image(m.group(3), base_dir) if m else None
        png = None
        if path:
            try:
                st = os.stat(path)
                if os.path.isfile(path) and os.access(path, os.R_OK):
                    png = _as_png(path, st)
            except OSError:
                png = None
        size = _png_size(png) if png else None
        if not size:
            out.append(ln)
            continue
        key = (path, st.st_mtime_ns, st.st_size)
        if key not in ids:
            ids[key] = _image_id(path, st, set(ids.values()))
        images.append({'id': ids[key], 'png': os.path.abspath(png),
                       'w': size[0], 'h': size[1], 'alt': m.group(2).strip()})
        # Blank lines on both sides make the marker its own paragraph, so glow
        # emits it as one line (and a list above it ends before it).
        out.extend(('', m.group(1) + IMG_MARK + str(len(images) - 1) + IMG_MARK, ''))
    return '\n'.join(out), images


def cell_pixels(override=None):
    """Cell size in pixels: --cell-px, then MDW_CELL_PX (both WxH), then the
    tty's own report (TIOCGWINSZ pixel fields divided by the grid; Ghostty
    fills them in), then a default with a typical 1:2 cell."""
    for spec in (override, os.environ.get('MDW_CELL_PX')):
        m = re.match(r'^\s*(\d+)\s*[xX]\s*(\d+)\s*$', spec or '')
        if m and int(m.group(1)) > 0 and int(m.group(2)) > 0:
            return int(m.group(1)), int(m.group(2))
    try:
        import fcntl
        import struct
        fd = os.open('/dev/tty', os.O_RDONLY)
        try:
            rows, cols, xpx, ypx = struct.unpack(
                'HHHH', fcntl.ioctl(fd, termios.TIOCGWINSZ, b'\0' * 8))
        finally:
            os.close(fd)
        if rows and cols and xpx >= cols and ypx >= rows:
            return xpx // cols, ypx // rows
    except Exception:
        pass
    return DEFAULT_CELL_PX


def image_grid(img, avail_cols, cell, pane_rows):
    """(cols, rows) for an image: the content width at the image's aspect
    ratio (small images scale up to fill it), never taller than
    IMG_MAX_FRACTION of the pane (narrowed to keep the aspect when capped)."""
    cw, ch = cell
    native = max(1, -(-img['w'] // cw))
    # Fill the text column (small screenshots look undersized next to the
    # text otherwise). Upscaling past native size is allowed; the
    # height cap below still keeps a tall image from swallowing the pane.
    cols = max(1, min(avail_cols, len(_DIACRITICS)))
    rows = max(1, round(cols * cw * img['h'] / (img['w'] * ch)))
    cap = max(3, int(pane_rows * IMG_MAX_FRACTION)) if pane_rows > 0 else rows
    cap = min(cap, len(_DIACRITICS))
    if rows > cap:
        rows = cap
        cols = max(1, min(cols, round(rows * ch * img['w'] / (img['h'] * cw))))
    return cols, rows


def place_images(text, images, width, cell, pane_rows, sidecar):
    """Replace every image marker line with its placeholder rows (plus a quiet
    alt-text caption, which also keeps the image findable with / search), and
    write one sidecar line per image id: id, cols, rows, png path."""
    placed = {}
    out = []
    for ln in text.split('\n'):
        vis = SGR.sub('', ln)
        m = _IMG_TOKEN.search(vis)
        if not m or int(m.group(1)) >= len(images):
            out.append(ln)
            continue
        img = images[int(m.group(1))]
        rest = (vis[:m.start()] + vis[m.end():]).strip()
        if rest:
            # glow merged the marker into other text (it should not, the marker
            # is its own paragraph): degrade to the alt text, never a stray mark.
            out.append(_IMG_TOKEN.sub(' ' + (img['alt'] or 'image') + ' ', ln))
            continue
        indent = len(vis) - len(vis.lstrip(' '))
        indent = min(indent, max(0, width - 8))
        iid = img['id']
        if iid not in placed:
            placed[iid] = (image_grid(img, max(1, width - indent), cell, pane_rows),
                           img['png'])
        (cols, rows), _ = placed[iid]
        fg = '\x1b[38;2;%d;%d;%dm' % ((iid >> 16) & 255, (iid >> 8) & 255, iid & 255)
        pad = ' ' * indent
        for r in range(rows):
            rd = IMG_PLACEHOLDER + _DIACRITICS[r]
            out.append(pad + fg + ''.join(rd + _DIACRITICS[c] for c in range(cols)) + SGR_OFF)
        if img['alt']:
            # "Image: " keeps the caption from ever equalling a heading title,
            # which mdw_copy_section.py's rendermap matches line by line.
            cap = ('Image: ' + img['alt'])[:max(1, width - indent)]
            out.append(pad + SGR_QUIET + '\x1b[3m' + cap + SGR_OFF)
    if sidecar:
        try:
            with open(sidecar, 'w', encoding='utf-8') as fh:
                for iid, ((cols, rows), png) in placed.items():
                    fh.write('%d\t%d\t%d\t%s\n' % (iid, cols, rows, png))
        except OSError as e:
            _log('image sidecar write failed (%s): %r' % (sidecar, e))
    return '\n'.join(out)


def prepare_source(path, images=False):
    """Return (glow_path, cleanup, urls, src, imgs). Rewrites to a temp file when the
    source needs it: (1) larger than MAX_BYTES -> truncated head with a visible
    notice, kept responsive; (2) contains raw control bytes or tabs ->
    sanitised so nothing executes on the terminal and tab-expanded width
    matches what is shown; (3) contains GitHub task items -> rewritten as plain
    list items carrying a task marker (see mark_task_items); (4) contains
    markdown links -> swapped for OSC 8 markers (see extract_links). Otherwise
    the original path and a no-op. `src` is the marker-free source text, used by
    join_wrapped to tell a glamour-split token from two separate words. With
    `images`, standalone image lines become marker paragraphs (see
    extract_images) and `imgs` lists them; otherwise `imgs` is empty."""
    noop = (path, lambda: None, [], '', [])
    try:
        size = os.path.getsize(path)
    except OSError:
        return noop
    truncated = size > MAX_BYTES
    read_n = MAX_BYTES if truncated else size
    try:
        with open(path, 'rb') as fh:
            raw = fh.read(read_n)
    except OSError:
        return noop
    text = raw.decode('utf-8', 'replace')

    if truncated:
        cut = text.rfind('\n')
        if cut > 0:
            text = text[:cut]

    text, changed = sanitize_text(text)
    if EMOJI_ENABLED:
        text, emo = convert_shortcodes(text)
        changed = changed or emo

    src = text
    text, tasks = mark_task_items(text)

    imgs = []
    if images:
        text, imgs = extract_images(text, os.path.dirname(os.path.abspath(path)))

    urls = []
    if LINKS_ENABLED:
        text, urls = extract_links(text)

    if not truncated and not changed and not tasks and not urls and not imgs:
        return (path, lambda: None, [], src, [])  # fast path: nothing to rewrite

    if truncated:
        # Balance code fences so the notice is not swallowed into a code block.
        if text.count('\n```') % 2 == 1 or text.startswith('```'):
            text += '\n```'
        omitted = size - read_n
        text += (
            '\n\n---\n\n> **Truncated for display.** Showing the first '
            f'{MAX_BYTES // 1024} KB; about {omitted // 1024} KB not shown. '
            'Raise MDW_MAX_BYTES to see more.\n'
        )

    import tempfile
    fd, tmp = tempfile.mkstemp(suffix='.md', prefix='mdw_head_')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(text)
    except OSError:
        return noop

    def cleanup():
        try:
            os.unlink(tmp)
        except OSError:
            pass

    return (tmp, cleanup, urls, src, imgs)


def main():
    cols, style_arg, path, img_opts = parse_args(sys.argv[1:])
    if not path:
        return
    style = resolve_style(style_arg)

    path, cleanup, urls, src, imgs = prepare_source(path, img_opts['on'])
    if img_opts['out']:
        # Empty sidecar up front, so a render that ends up placing nothing (or
        # falls back to raw) never leaves mdw reading the previous file's list.
        try:
            open(img_opts['out'], 'w').close()
        except OSError:
            pass
    try:
        _render(cols, style, path, urls, src, imgs, img_opts)
    except Exception:
        import traceback
        _log('EXCEPTION in _render (%s @ %s cols):\n%s' % (path, cols, traceback.format_exc()))
        _emit_raw(path)
    finally:
        cleanup()


def _render(cols, style, path, urls=(), src='', imgs=(), img_opts=None):
    try:
        width = int(cols)
    except (TypeError, ValueError):
        width = 0
    # Ask glow to wrap 2 columns NARROWER than the fold limit: glamour rounds
    # its wrap a column or two past the requested width (blockquotes and list
    # items), and when its lines land just over the fold limit the fold splits
    # exactly one word onto a continuation line (the recurring "these"/"fail"
    # single-word orphans). With the headroom, glamour's overshoot fits inside
    # the limit untouched and the fold only fires on genuinely unbreakable
    # tokens (long paths/URLs).
    glow_w = str(width - 2) if width > 22 else cols
    glow_cmd = ['glow', '-s', style, '-w', glow_w, path]

    def emit(out):
        # Re-add gutters glamour dropped on wrapped blockquote lines, then fold to
        # the target width so nothing (glamour list overshoot, a long unbreakable
        # path) can exceed the pane and hard-wrap mid-word. Link markers become
        # OSC 8 hyperlinks only after folding (markers are width 1, the escapes
        # are width 0, so folded lines can only shrink); any marker that somehow
        # survives is stripped so tofu never reaches the pane.
        out = drop_blank_table_rows(out)
        if width > 0:
            out = expand_rules(out, width)
        out = restyle_quote_gutters(out)
        out = regutter_quote_orphans(out)
        if width > 0:
            out = rewrap_quote_runs(out, width, src)
            # List items are re-wrapped BEFORE the prose widow pass: they own
            # their whole block (marker line plus continuations), and once their
            # continuations are indented under the item text the widow pass skips
            # them as indented lines, exactly as it already skips quotes.
            out = rewrap_list_items(out, width, src)
            # Widow check and re-wrap run at glow's OWN wrap width (width - 2),
            # not the fold limit: maximality only holds at the width glamour
            # wrapped to, and checking against the looser fold limit would flag
            # healthy paragraphs whose next word is 1-2 chars.
            out = rewrap_prose_widows(out, int(glow_w), src)
            # Headings last of the layout passes: their rules are exactly the
            # pane's width, so they must be drawn after every re-wrap and before
            # the fold (which leaves a line sitting exactly AT the limit alone).
            out = style_headings(out, width)
            out = fit_width(out, width)
        if urls:
            out = linkify(out, urls)
        # Images last: placeholder cells are 1 column each but 3 code points,
        # so any width pass (fold_line counts code points) would shred them.
        if imgs:
            out = place_images(out, imgs, width if width > 0 else 80,
                               cell_pixels(img_opts.get('cell')),
                               img_opts.get('pane_rows') or 0, img_opts.get('out'))
        out = _PUA_STRAY.sub('', out)
        sys.stdout.write(out)

    # 1. Primary: glow under a pty -> exact Rose Pine truecolour, no pills.
    out = glow_pty(glow_cmd)
    if out is not None:
        emit(out)
        return

    # 2. glow via pipe (16-colour ANSI, still colour-adaptive and pill-free).
    _log('glow_pty returned None (%s @ %s cols); trying pipe' % (path, cols))
    out = run_pipe(glow_cmd)
    if out is not None:
        emit(out)
        return

    # 3. mdcat --ansi (reintroduces the H1 pill, but never blanks).
    _log('glow pipe returned None (%s @ %s cols); trying mdcat' % (path, cols))
    out = run_pipe(['mdcat', '--ansi', '--columns', cols, path])
    if out is not None:
        _log('rendered via mdcat fallback (%s)' % path)
        emit(TERM_QUERY.sub('', out))
        return

    # 4. Raw text, last resort, with a visible banner so it is never silent.
    _log('ALL renderers failed (%s @ %s cols); raw fallback' % (path, cols))
    _emit_raw(path)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        _log('EXCEPTION in main:\n%s' % traceback.format_exc())
        _, _, p, _ = parse_args(sys.argv[1:])
        if p:
            _emit_raw(p)
