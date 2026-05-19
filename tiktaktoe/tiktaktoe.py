"""A tiny command-line tic-tac-toe game."""


WINNING_LINES = (
    (0, 1, 2),
    (3, 4, 5),
    (6, 7, 8),
    (0, 3, 6),
    (1, 4, 7),
    (2, 5, 8),
    (0, 4, 8),
    (2, 4, 6),
)


def show_board(board):
    """Print the current board."""
    print()
    print(f" {board[0]} | {board[1]} | {board[2]} ")
    print("---+---+---")
    print(f" {board[3]} | {board[4]} | {board[5]} ")
    print("---+---+---")
    print(f" {board[6]} | {board[7]} | {board[8]} ")
    print()


def winner(board):
    """Return the winning player mark, or None if there is no winner yet."""
    for a, b, c in WINNING_LINES:
        if board[a] == board[b] == board[c]:
            return board[a]
    return None


def get_move(board, player):
    """Ask the current player for a valid move."""
    while True:
        move = input(f"Player {player}, choose a square (1-9): ").strip()

        if not move.isdigit():
            print("Please enter a number from 1 to 9.")
            continue

        index = int(move) - 1
        if index < 0 or index > 8:
            print("Please enter a number from 1 to 9.")
            continue

        if board[index] in ("X", "O"):
            print("That square is already taken.")
            continue

        return index


def play():
    """Run one game of tic-tac-toe."""
    board = [str(number) for number in range(1, 10)]
    current_player = "X"

    print("Welcome to TikTakToe!")

    for _ in range(9):
        show_board(board)
        move = get_move(board, current_player)
        board[move] = current_player

        if winner(board):
            show_board(board)
            print(f"Player {current_player} wins!")
            return

        current_player = "O" if current_player == "X" else "X"

    show_board(board)
    print("It's a tie!")


if __name__ == "__main__":
    play()
