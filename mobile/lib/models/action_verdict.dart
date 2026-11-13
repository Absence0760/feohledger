/// One workflow action's verdict from `GET /api/invoices/{id}/review-actions`:
/// whether the endpoint would accept the signed-in caller right now, and, when
/// not, the coded refusal (`{code, message, params}`) it would answer with.
///
/// The server computes it from the very gate the endpoint raises from (for
/// the recall, `services/review.recall_refusal` — backend decisions §374), so
/// the app offers an action from this and never restates the rule.
///
/// **Fails closed:** a response that does not carry the action's key (an older
/// backend, a mocked or failed read) is [answered] `false`, which offers
/// nothing.
class ActionVerdict {
  /// Whether the server gave a verdict for this action at all.
  final bool answered;

  /// The coded refusal, or `null` when the server would accept the action.
  final Map<String, dynamic>? refusal;

  const ActionVerdict._(this.answered, this.refusal);

  /// No verdict: nothing is offered.
  static const unanswered = ActionVerdict._(false, null);

  /// The verdict under [key] of a `review-actions` response.
  factory ActionVerdict.fromVerdicts(Map<String, dynamic> verdicts, String key) {
    if (!verdicts.containsKey(key)) return unanswered;
    final value = verdicts[key];
    if (value == null) return const ActionVerdict._(true, null);
    if (value is Map) {
      return ActionVerdict._(true, Map<String, dynamic>.from(value));
    }
    return unanswered;
  }

  /// The server would accept the action now.
  bool get allowed => answered && refusal == null;

  /// The refusal's code, or `null`.
  String? get code {
    final c = refusal?['code'];
    return c is String ? c : null;
  }

  /// Whether to show the control at all: answered, and not refused for a
  /// reason the reader can never act on — their role, or one of [hiddenCodes]
  /// (a status the action does not start from).
  bool isOffered({Set<String> hiddenCodes = const {}}) {
    if (!answered) return false;
    final c = code;
    if (c == null) return refusal == null;
    return c != 'role_not_permitted' && !hiddenCodes.contains(c);
  }
}
