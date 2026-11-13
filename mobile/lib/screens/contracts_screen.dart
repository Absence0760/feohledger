import 'package:material_ui/material_ui.dart';
import 'package:flutter/scheduler.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/contract.dart';
import 'package:feohledger_mobile/screens/contract_detail_screen.dart';
import 'package:feohledger_mobile/stores/contract_store.dart';
import 'package:feohledger_mobile/utils/debouncer.dart';
import 'package:feohledger_mobile/widgets/contract_list_tile.dart';

class ContractsScreen extends StatefulWidget {
  const ContractsScreen({super.key});

  @override
  State<ContractsScreen> createState() => _ContractsScreenState();
}

class _ContractsScreenState extends State<ContractsScreen> {
  final _searchController = TextEditingController();
  final _searchDebouncer = Debouncer();

  @override
  void initState() {
    super.initState();
    SchedulerBinding.instance.addPostFrameCallback((_) {
      ContractStore.instance.fetch();
    });
  }

  @override
  void dispose() {
    _searchDebouncer.cancel();
    _searchController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Scaffold(
      appBar: AppBar(
        title: Text(l.contractsTitle),
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(56),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
            child: SearchBar(
              controller: _searchController,
              hintText: l.contractsSearchHint,
              leading: const Icon(Icons.search, size: 20),
              onChanged: (q) => _searchDebouncer.run(
                () => ContractStore.instance.setSearch(q.isEmpty ? null : q),
              ),
              elevation: WidgetStateProperty.all(0),
            ),
          ),
        ),
      ),
      body: Column(
        children: [
          // Status filter chips
          SizedBox(
            height: 48,
            child: ListenableBuilder(
              listenable: ContractStore.instance,
              builder: (context, _) {
                final current = ContractStore.instance.statusFilter;
                return ListView(
                  scrollDirection: Axis.horizontal,
                  padding: const EdgeInsets.symmetric(horizontal: 12),
                  children: [
                    _filterChip(l.commonAll, null, current),
                    _filterChip(l.contractsFilterDraft, 'draft', current),
                    _filterChip(l.contractsFilterActive, 'active', current),
                    _filterChip(l.contractsFilterExpired, 'expired', current),
                    _filterChip(
                        l.contractsFilterTerminated, 'terminated', current),
                    _filterChip(
                        l.contractsFilterCancelled, 'cancelled', current),
                  ],
                );
              },
            ),
          ),

          // Contract list
          Expanded(
            child: ListenableBuilder(
              listenable: ContractStore.instance,
              builder: (context, _) {
                final store = ContractStore.instance;

                if (store.loading && store.contracts.isEmpty) {
                  return const Center(child: CircularProgressIndicator());
                }

                if (store.contracts.isEmpty) {
                  return Center(child: Text(l.contractsEmpty));
                }

                return RefreshIndicator(
                  onRefresh: store.fetch,
                  child: ListView.separated(
                    itemCount: store.contracts.length,
                    separatorBuilder: (_, _) => const Divider(height: 1),
                    itemBuilder: (context, index) {
                      final contract = store.contracts[index];
                      return ContractListTile(
                        contract: contract,
                        onTap: () => _openDetail(contract),
                      );
                    },
                  ),
                );
              },
            ),
          ),
        ],
      ),
    );
  }

  Widget _filterChip(String label, String? value, String? current) {
    final selected = current == value;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 4),
      child: FilterChip(
        label: Text(label),
        selected: selected,
        onSelected: (_) => ContractStore.instance.setStatusFilter(value),
      ),
    );
  }

  void _openDetail(Contract contract) {
    Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => ContractDetailScreen(contractId: contract.id),
      ),
    );
  }
}
