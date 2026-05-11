import matplotlib.pyplot as plt
import io

class FinanceService:
    @staticmethod
    def check_dynamic_budget(user_id: int, new_expense: float, category: str) -> str:
        """
        Calculates if an expense breaks the budget. 
        If it does, generates a positive reallocation message (El Fin de la 'Espiral de Culpa').
        """
        # TODO: Lookup DB for budgets vs expenses
        return "¡Buen registro! Te pasaste un poco en ocio, pero ajustamos el límite de ropa de este mes para que sigas en carrera. ¡Vamos bien!"

    @staticmethod
    def generate_expense_chart(expenses_by_category: dict) -> bytes:
        """
        Generates a basic pie chart and returns it as bytes.
        """
        labels = list(expenses_by_category.keys())
        sizes = list(expenses_by_category.values())
        
        fig, ax = plt.subplots(figsize=(6, 6))
        ax.pie(sizes, labels=labels, autopct='%1.1f%%', startangle=90)
        ax.axis('equal')
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png')
        buf.seek(0)
        
        plt.close(fig)
        return buf.read()
