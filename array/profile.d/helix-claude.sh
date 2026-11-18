# Claude Code kept on the array (the home folder is in RAM): link it back in at every login.
_cc=/mnt/user/appdata/claude-code
if [ -d "$_cc/share/versions" ] && [ -n "$HOME" ] && [ -w "$HOME" ]; then
    mkdir -p "$HOME/.local/bin" "$HOME/.local/share"
    [ -L "$HOME/.local/share/claude" ] || ln -sfn "$_cc/share" "$HOME/.local/share/claude"
    _v=$(ls -1 "$_cc/share/versions" 2>/dev/null | sort -V | tail -1)
    [ -n "$_v" ] && ln -sfn "$HOME/.local/share/claude/versions/$_v" "$HOME/.local/bin/claude"
    export CLAUDE_CONFIG_DIR="$_cc/config"
    case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac
    unset _v
fi
unset _cc
